#!/usr/bin/env python3
"""The live demo: everything running at once, with the dashboard showing
it happen in real time.

Starts the inference service, the controller (full P2_ML_RL system), the
dashboard backend, and a real Mininet network; lets telemetry settle;
then injects a real fault on the link the traffic is actually using and
leaves everything running so the dashboard can be watched reacting.

Nothing here is staged. The fault is a real `tc netem` change, the
detection is the trained model's own output, the reroute is chosen by
the trained RL policy, and the verification is a real measurement. If
the system fails to detect, the dashboard will show that too -- this
script has no way to fake a success.

The dashboard binds to 0.0.0.0 inside the VM so it can be opened from
the host's browser at the VM's address (printed on startup).

Run inside the VM under system python3 (Mininet is bound to it):
    sudo python3 demo/run_live_demo.py                # fault after 90s
    sudo python3 demo/run_live_demo.py --fault-after 0   # wait for Enter
    sudo python3 demo/run_live_demo.py --no-fault        # just run the network

Ctrl+C stops everything and cleans up.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from common.config_loader import load_config, resolve_venv_ctrl  # noqa: E402
from database.db import Database  # noqa: E402
from network.failure_injector import FailureInjector  # noqa: E402
from network.topo_builder import build_network, load_topology_spec  # noqa: E402

VENV_ML = Path(os.environ.get("VENV_ML", REPO_ROOT / "venv-ml"))
VENV_CTRL = resolve_venv_ctrl()

BANNER = "=" * 66

# Deliberately identical to ml/dataset/generate_dataset.py's background
# load. The model was trained under continuous traffic between these
# pairs; on an idle network a bandwidth fault has almost nothing to act
# on, so the features it reads look nothing like what it learned.
#
# Measured: with no background traffic the degraded link ranked SIXTH of
# eight by predicted probability (0.676, against 0.969 on a healthy
# off-path link) and no incident was ever opened. Matching the training
# load is a precondition for the detector behaving as evaluated, not a
# presentation trick.
BACKGROUND_TRAFFIC_PAIRS = [("h1", "h6"), ("h2", "h5"), ("h3", "h4")]
BACKGROUND_UDP_MBIT = 2

# Traffic processes we started, so cleanup can actually stop them.
_BACKGROUND_PROCS: list = []


def log(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _preflight(cfg, dashboard_port: int) -> bool:
    """Refuse to start on top of a previous run.

    This exact situation silently wasted two demo runs: an orphaned
    controller from an earlier attempt still held OpenFlow port 6653, so
    Mininet's switches connected to THAT controller, which was writing to
    a different database. The demo looked alive -- switches up, service
    answering -- while its own database stayed empty and it eventually
    reported "the controller never installed a path". Failing loudly here
    is far better than that, and much better than discovering it in front
    of an audience.
    """
    busy = [
        (name, port)
        for name, port in (
            ("OpenFlow (controller)", cfg.controller.of_listen_port),
            ("inference service", cfg.ml_rl_service.port),
            ("dashboard", dashboard_port),
        )
        if _port_in_use(port)
    ]
    if not busy:
        return True

    log()
    log(BANNER)
    log("  CANNOT START -- something is already running")
    for name, port in busy:
        log(f"    port {port} ({name}) is in use")
    log()
    log("  A leftover process from a previous run will silently hijack")
    log("  this one. Clear it first:")
    log()
    log("    sudo pkill -f ryu.cmd.manager; sudo pkill -f uvicorn; sudo mn -c")
    log()
    log(BANNER)
    return False


def _vm_address() -> str:
    """Best-effort outward-facing address, so the printed URL is one the
    host's browser can actually reach rather than 127.0.0.1."""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
        s.close()
        return addr
    except Exception:
        return "127.0.0.1"


def _start_background_traffic(net, duration_s: float) -> None:
    """Long-lived UDP flows between several host pairs, so telemetry has
    continuous load for a fault to actually show up against. Fire and
    forget -- they live in Mininet's own host namespaces for the whole
    demo."""
    for client_id, server_id in BACKGROUND_TRAFFIC_PAIRS:
        try:
            client = net.get(client_id)
            server = net.get(server_id)
        except KeyError:
            continue  # a topology without these hosts is fine, just quieter
        # NOT -D: a daemonised iperf3 detaches from the host namespace and
        # survives net.stop(), so repeated demo runs leave a growing pile
        # of orphans (9 of them, up to 2h43m old, were found on this VM).
        # Held as a child instead, and killed in the cleanup below.
        _BACKGROUND_PROCS.append(server.popen(["iperf3", "-s", "-p", "5301"]))
        _BACKGROUND_PROCS.append(client.popen([
            "iperf3", "-c", server.IP(), "-p", "5301", "-u",
            "-b", f"{BACKGROUND_UDP_MBIT}M", "-t", str(int(duration_s)),
        ]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topo", default=None, help="topology name (default: from config)")
    parser.add_argument("--manual", action="store_true",
                        help="settle first, then wait for Enter before injecting "
                             "-- lets you choose the moment in front of an audience")
    parser.add_argument("--fault-after", type=float, default=90.0,
                        help="seconds to settle before injecting; 0 waits for Enter")
    parser.add_argument("--fault-type", default="gradual_degradation")
    parser.add_argument("--fault-duration", type=float, default=600.0)
    parser.add_argument("--no-fault", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    cfg = load_config()
    topo_name = args.topo or cfg.network.topology_name
    work = REPO_ROOT / "results" / "demo_live"
    work.mkdir(parents=True, exist_ok=True)
    db_path = work / "live.db"

    # Fresh database so the dashboard shows THIS demo, not a past one.
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    demo_cfg = yaml.safe_load((REPO_ROOT / "config" / "config.yaml").read_text())
    demo_cfg["experiment"]["system"] = "P2_ML_RL"
    demo_cfg["database"]["path"] = str(db_path)
    demo_cfg["network"]["topology_name"] = topo_name
    cfg_path = work / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(demo_cfg, sort_keys=False))
    env = dict(os.environ, AI_SDN_CONFIG=str(cfg_path))

    service = controller = backend = net = None
    try:
        log(BANNER)
        log("  AI-Driven Self-Healing Network -- LIVE DEMO")
        log(BANNER)

        if not _preflight(cfg, args.port):
            return 1

        step("starting the ML/RL inference service...")
        service = subprocess.Popen(
            [str(VENV_ML / "bin" / "python3"), "-m", "uvicorn", "service.ml_rl_service:app",
             "--host", cfg.ml_rl_service.host, "--port", str(cfg.ml_rl_service.port)],
            cwd=str(REPO_ROOT), env=env,
            stdout=open(work / "service.log", "w"), stderr=subprocess.STDOUT,
        )
        time.sleep(8)
        if service.poll() is not None:
            log(f"  the inference service exited -- see {work / 'service.log'}")
            return 1

        step("starting the controller (P2_ML_RL: prediction + RL routing)...")
        controller_log = open(work / "controller.log", "w")
        controller = subprocess.Popen(
            [str(VENV_CTRL / "bin" / "python3.9"), "-u", "-m", "ryu.cmd.manager",
             "--observe-links", "controller.main_app", "controller.telemetry_collector"],
            cwd=str(REPO_ROOT), env=env, stdout=controller_log, stderr=subprocess.STDOUT,
        )
        time.sleep(10)
        if controller.poll() is not None:
            log(f"  controller failed to start -- see {work / 'controller.log'}")
            return 1

        step("starting the dashboard...")
        backend = subprocess.Popen(
            [str(VENV_ML / "bin" / "python3"), "-m", "uvicorn", "backend.api:app",
             "--host", "0.0.0.0", "--port", str(args.port)],
            cwd=str(REPO_ROOT), env=env,
            stdout=open(work / "dashboard.log", "w"), stderr=subprocess.STDOUT,
        )
        time.sleep(5)

        step(f"building the network ({topo_name})...")
        spec = load_topology_spec(topo_name)
        net = build_network(spec, "127.0.0.1", cfg.controller.of_listen_port, fail_mode="secure")
        net.start()

        url = f"http://{_vm_address()}:{args.port}"
        log()
        log(BANNER)
        log(f"  DASHBOARD:   {url}")
        log("  Open that in your browser now.")
        log(BANNER)
        log()

        step("waiting for the controller to discover the topology and install a path...")
        deadline = time.time() + 90
        active_path = None
        while time.time() < deadline:
            try:
                with Database(db_path) as db:
                    row = db.query_one(
                        "SELECT new_path FROM route_changes WHERE changed = 1 "
                        "ORDER BY timestamp DESC LIMIT 1"
                    )
                if row and row["new_path"]:
                    active_path = row["new_path"]
                    break
            except Exception:
                pass
            time.sleep(2)

        if not active_path:
            log("  the controller never installed a path -- check controller.log")
            return 1
        step(f"the controller installed {active_path}")

        # Order matters, and getting it wrong cost a full demo run:
        #   1. LLDP has to converge and the controller has to install a
        #      path BEFORE traffic starts. Starting iperf3 first floods
        #      the control plane with ARP while discovery is still
        #      running, and the path never gets installed.
        #   2. pingAll populates ARP/MAC tables, exactly as
        #      generate_dataset.py does before its own background load --
        #      without it the iperf3 clients stall on unresolved hosts.
        step("populating ARP tables (pingAll)...")
        net.pingAll(timeout="1")

        step("starting background traffic (the same load the model was trained under)...")
        _start_background_traffic(net, args.fault_duration + args.fault_after + 600)
        time.sleep(5)
        step("traffic is flowing")

        if args.no_fault:
            log("\n  --no-fault: leaving the network running. Ctrl+C to stop.\n")
            while True:
                time.sleep(3600)

        if args.fault_after > 0:
            step(f"letting telemetry settle for {args.fault_after:.0f}s before breaking anything")
            step("(the model reads TRENDS, so it needs a stable baseline first)")
            time.sleep(args.fault_after)
        if args.manual or args.fault_after <= 0:
            # Settle first, THEN hand over the trigger. `--fault-after 0`
            # alone skips the settle entirely, which is the wrong thing to
            # do before a live audience: the model reads trends, so
            # breaking a link two seconds after startup asks it to detect
            # a change from a baseline it has not established. --manual
            # keeps the settle and lets you choose the moment.
            log("")
            log("  Settled. Press Enter to break a link...")
            input()

        switches = active_path.split(",")
        sw_a, sw_b = switches[0], switches[1]
        log()
        log(BANNER)
        log(f"  BREAKING {sw_a}-{sw_b}  ({args.fault_type})")
        log("  Watch the dashboard. Nobody touches anything from here.")
        log(BANNER)
        log()

        injector = FailureInjector(net, spec, ground_truth_path=str(work / "faults.jsonl"))
        threading.Thread(
            target=injector.inject,
            args=(sw_a, sw_b, args.fault_type, args.fault_duration),
            daemon=True,
        ).start()

        # Narrate what the system does, read from its own database -- so
        # the terminal and the dashboard agree, and neither is scripted.
        fault_start = time.time()
        seen_incident = seen_reroute = seen_verify = False
        while True:
            time.sleep(3)
            try:
                with Database(db_path) as db:
                    inc = db.query_one(
                        "SELECT * FROM incidents WHERE incident_id LIKE 'inc-%' AND opened_at >= ? "
                        "ORDER BY opened_at LIMIT 1", (fault_start,))
                    rr = db.query_one(
                        "SELECT * FROM route_changes WHERE changed = 1 AND timestamp >= ? "
                        "ORDER BY timestamp DESC LIMIT 1", (fault_start,))
                    ver = db.query_one(
                        "SELECT * FROM verification_results WHERE timestamp >= ? "
                        "ORDER BY timestamp DESC LIMIT 1", (fault_start,))
            except Exception:
                continue

            if inc is not None and not seen_incident:
                seen_incident = True
                step(f"DETECTED  {inc['link_id']} -- {inc['reason']}")
            if rr is not None and not seen_reroute:
                seen_reroute = True
                who = "the RL policy" if rr["selector"] == "P2_ML_RL" else "the heuristic fallback"
                step(f"REROUTED  {rr['old_path']} -> {rr['new_path']}  (chosen by {who})")
            if ver is not None and not seen_verify:
                seen_verify = True
                step(f"VERIFIED  {ver['classification']} after {ver['retry_count']} retries")
                log()
                log(BANNER)
                log(f"  Recovered in {ver['timestamp'] - fault_start:.1f}s, with no human involved.")
                log("  The dashboard shows the full story. Ctrl+C when finished.")
                log(BANNER)
                log()

    except KeyboardInterrupt:
        log("\n  stopping...")
    finally:
        for proc in _BACKGROUND_PROCS:
            try:
                proc.kill()
            except Exception:
                pass
        _BACKGROUND_PROCS.clear()
        if net is not None:
            try:
                net.stop()
            except Exception:
                pass
        for proc in (backend, controller, service):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("  cleaned up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
