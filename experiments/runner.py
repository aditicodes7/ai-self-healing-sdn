#!/usr/bin/env python3
"""Runs Phase 22 trials: one (experiment, system, fault, seed) at a time,
against real Mininet, and records every measured value into the
`experiment_runs` table.

Nothing here estimates or interpolates. Throughput comes from iperf3's
own JSON, loss and RTT from ping, and the reroute/recovery timeline from
what the controller actually wrote to the database during the trial. A
trial that fails to produce a measurement records null for it, never a
filled-in guess.

Each trial is fully isolated: its own database file, its own generated
config (so the exact settings are reproducible from the artefact), and
its own controller/service processes, all torn down afterwards.

Run inside the VM under system python3 (Mininet is bound to it):
    sudo python3 experiments/runner.py --experiment E1 --seeds 1 2
    sudo python3 experiments/runner.py --pilot          # smallest useful check
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from common.config_loader import load_config  # noqa: E402
from database.db import Database  # noqa: E402
from experiments.definitions import by_id  # noqa: E402
from network.failure_injector import FailureInjector  # noqa: E402
from network.topo_builder import build_network, load_topology_spec  # noqa: E402

VENV_ML = Path(os.environ.get("VENV_ML", REPO_ROOT / "venv-ml"))
VENV_CTRL = Path(os.environ.get("VENV_CTRL", Path.home() / "venv-ctrl-ai-self-healing-sdn"))

SETTLE_BEFORE_FAULT_S = 90   # matches tests/e2e; startup transient inverts latency trends

# Continuous background load, identical to ml/dataset/generate_dataset.py
# and demo/run_live_demo.py.
#
# Without it the runner only produced three 10-second iperf3 bursts, so
# links were idle most of the time -- a regime the model never trained
# on. Measured consequence: link_failure scored 0/5 here while the live
# demo (which does run this load) detected the SAME fault in 6s with 3 of
# 3 readings over threshold. On an idle link, killing it changes nothing
# observable, because utilization was already zero. Every E2 rate
# measured before this was depressed by the harness, not only by the
# detector.
BACKGROUND_TRAFFIC_PAIRS = [("h1", "h6"), ("h2", "h5"), ("h3", "h4")]
BACKGROUND_UDP_MBIT = 2
FAULT_DURATION_S = 150
POST_FAULT_OBSERVE_S = 30
IPERF_SAMPLE_S = 10


def log(msg: str) -> None:
    print(f"[exp {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _write_trial_config(
    trial_dir: Path, system: str, db_path: Path, ablate_gates: bool = False
) -> Path:
    """Materialise the exact config this trial ran under, so a result can
    always be traced back to the settings that produced it.

    `ablate_gates` is E5's second arm. E5 claims the persistence and
    stability layer is load-bearing, which can only be shown by running
    with it and without it -- and the first E5 batch ran neither, because
    nothing here implemented the ablation and all five trials used the
    default gates. Five more ordinary trials cannot support a claim about
    what happens when you remove something.

    Removing the layer means: act on a single prediction instead of three
    consecutive ones, and drop the three brakes on rerouting -- the
    cooldown between reroutes, the hold-down that keeps a new route in
    place, and the minimum improvement a candidate must offer to be worth
    switching to.
    """
    base = yaml.safe_load((REPO_ROOT / "config" / "config.yaml").read_text())
    base["experiment"]["system"] = system
    base["database"]["path"] = str(db_path)
    if ablate_gates:
        base["persistence"]["consecutive"] = 1
        base["stability"]["cooldown_s"] = 0
        base["stability"]["route_hold_s"] = 0
        base["stability"]["min_improvement"] = 0.0
    out = trial_dir / "config.yaml"
    out.write_text(yaml.safe_dump(base, sort_keys=False))
    return out


def _start_background_traffic(net, duration_s: float, mbit: int | None = None) -> list:
    """Continuous load so a fault has something to act on. Returns the
    processes so the trial can stop them -- a daemonised iperf3 outlives
    its Mininet namespace and accumulates across trials."""
    procs = []
    for client_id, server_id in BACKGROUND_TRAFFIC_PAIRS:
        try:
            client, server = net.get(client_id), net.get(server_id)
        except KeyError:
            continue
        procs.append(server.popen(["iperf3", "-s", "-p", "5301"]))
        procs.append(client.popen([
            "iperf3", "-c", server.IP(), "-p", "5301", "-u",
            "-b", f"{mbit or BACKGROUND_UDP_MBIT}M", "-t", str(int(duration_s)),
        ]))
    return procs


def _measure_flow(net, duration_s: int, port: int) -> dict:
    """One iperf3 h1->h6 sample plus a ping sample. Returns nulls for
    anything that failed rather than substituting a number.

    Both commands are wrapped in `timeout`. Mininet's `cmd()` blocks
    until the command exits and offers no deadline of its own, so a
    stalled iperf3 client hangs the whole trial: one E2 trial sat here
    for 18 minutes (a 5-minute trial took 21), and the "after" sample it
    eventually produced described the network a quarter of an hour past
    the point it was meant to measure. Silent, and invisible in the
    trial record -- only the log timestamps showed it. A measurement
    that cannot be taken in its window must return None, which the
    caller already handles; taking it late is worse than not taking it,
    because a late number still looks like a valid one.

    `--connect-timeout` bounds the setup phase specifically, which is
    where a client stalls when the path is lossy -- exactly the
    condition several fault types create.
    """
    h1, h6 = net.get("h1"), net.get("h6")
    result: dict = {"throughput_bps": None, "loss_percent": None,
                    "rtt_avg_ms": None, "measure_elapsed_s": None,
                    "measure_timed_out": False}
    started = time.time()

    server = h6.popen(["iperf3", "-s", "-p", str(port), "-1", "-J"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    try:
        out = h1.cmd(
            f"timeout {duration_s + 20} iperf3 -c {h6.IP()} -p {port} "
            f"-t {duration_s} --connect-timeout 5000 -J"
        )
        start = out.find("{")
        if start != -1:
            data = json.loads(out[start:out.rfind("}") + 1])
            if "error" not in data:
                result["throughput_bps"] = data["end"]["sum_received"]["bits_per_second"]
    except (ValueError, KeyError):
        pass  # leave as None -- an unmeasured value is never invented
    finally:
        server.terminate()

    try:
        ping_out = h1.cmd(f"timeout 30 ping -c 10 -i 0.2 -W 1 {h6.IP()}")
        for line in ping_out.splitlines():
            if "packet loss" in line:
                result["loss_percent"] = float(line.split("%")[0].split()[-1])
            if line.startswith("rtt") or line.startswith("round-trip"):
                result["rtt_avg_ms"] = float(line.split("=")[1].split("/")[1])
    except (ValueError, IndexError):
        pass

    # Recorded so an over-long measurement is visible in the trial data
    # itself, not only in log timestamps nobody re-reads. The budget is
    # the two `timeout` values plus the 1s server settle and a little
    # slack; exceeding it means something outside those bounds stalled.
    result["measure_elapsed_s"] = time.time() - started
    result["measure_timed_out"] = result["measure_elapsed_s"] > (duration_s + 20) + 30 + 5

    return result


def _timeline_from_db(
    db_path: Path, fault_start: float, service_killed_at: float | None = None
) -> dict:
    """What the controller actually did, read back from its own tables."""
    with Database(db_path) as db:
        incident = db.query_one(
            "SELECT * FROM incidents WHERE incident_id LIKE 'inc-%' AND opened_at >= ? "
            "ORDER BY opened_at LIMIT 1",
            (fault_start,),
        )
        reroutes = db.query(
            "SELECT * FROM route_changes WHERE changed = 1 AND timestamp >= ? ORDER BY timestamp",
            (fault_start,),
        )
        verification = None
        if incident is not None:
            verification = db.query_one(
                "SELECT * FROM verification_results WHERE incident_id = ? ORDER BY timestamp DESC LIMIT 1",
                (incident["incident_id"],),
            )
        n_predictions = db.query_one(
            "SELECT COUNT(*) c FROM predictions WHERE timestamp >= ?", (fault_start,)
        )["c"]

        # E8's claim is that an unreachable model produces NO prediction
        # rather than a guessed one, and the only way to show that is to
        # count what happened after the service died. Without these, E8
        # reported "0 fallbacks" and looked like a null result, while the
        # database showed the safety gate working correctly -- the
        # evidence existed and the experiment simply never looked at it.
        n_predictions_after_kill = None
        fallbacks_after_kill = []
        if service_killed_at is not None:
            n_predictions_after_kill = db.query_one(
                "SELECT COUNT(*) c FROM predictions WHERE timestamp >= ?", (service_killed_at,)
            )["c"]
            fallbacks_after_kill = [
                r["rl_fallback_reason"]
                for r in db.query(
                    "SELECT * FROM route_changes WHERE rl_fallback = 1 AND timestamp >= ? "
                    "ORDER BY timestamp", (service_killed_at,)
                )
            ]

    detected_at = incident["opened_at"] if incident else None
    verified_at = verification["timestamp"] if verification else None
    return {
        "detected": incident is not None,
        "incident_id": incident["incident_id"] if incident else None,
        "incident_status": incident["status"] if incident else None,
        # Seconds from fault onset to the controller opening an incident.
        "detection_latency_s": (detected_at - fault_start) if detected_at else None,
        # Seconds from fault onset to a verified recovery.
        "recovery_time_s": (verified_at - fault_start) if verified_at else None,
        "verification": verification["classification"] if verification else None,
        "reroute_count": len(reroutes),
        "selectors": [r["selector"] for r in reroutes],
        "rl_fallbacks": [r["rl_fallback_reason"] for r in reroutes if r["rl_fallback"]],
        "service_killed_at": service_killed_at,
        "n_predictions_after_service_kill": n_predictions_after_kill,
        "rl_fallbacks_after_service_kill": fallbacks_after_kill,
        "final_path": reroutes[-1]["new_path"] if reroutes else None,
        "n_predictions_during_fault": n_predictions,
    }


def run_trial(experiment_id: str, system: str, fault_type: str, seed: int,
              results_root: Path, kill_service_mid_run: bool = False,
              ablate_gates: bool = False, background_mbit: int | None = None) -> dict:
    cfg = load_config()
    # The suffix keeps the two E5 arms apart in the shared results
    # database and in every table built from it -- without it the
    # ablation would be averaged into the control.
    suffix = "_nogates" if ablate_gates else ""
    # E7 sweeps offered load. Its claim is that behaviour is
    # characterised ACROSS load rather than at one convenient operating
    # point, and the first E7 batch ran every trial at the same hardcoded
    # 2 Mbit -- so it measured one point and claimed a curve. The level
    # goes in the trial id because a sweep whose points cannot be told
    # apart afterwards is not a sweep.
    if background_mbit is not None:
        suffix += f"_bg{background_mbit}"
    trial_id = f"{experiment_id}_{system}_{fault_type}_seed{seed}{suffix}"
    trial_dir = results_root / trial_id
    if trial_dir.exists():
        shutil.rmtree(trial_dir)
    trial_dir.mkdir(parents=True)

    db_path = trial_dir / "network.db"
    trial_config = _write_trial_config(trial_dir, system, db_path, ablate_gates=ablate_gates)

    log(f"=== {trial_id} ===")
    started_at = time.time()
    service = controller = net = None
    bg_procs: list = []
    record: dict = {"trial_id": trial_id, "error": None}

    try:
        env = dict(os.environ, AI_SDN_CONFIG=str(trial_config))

        # B1 never consults the model, but the service still runs so that
        # every arm sees identical host load -- otherwise B1 would enjoy a
        # quieter machine and the comparison would be confounded.
        service = subprocess.Popen(
            [str(VENV_ML / "bin" / "python3"), "-m", "uvicorn", "service.ml_rl_service:app",
             "--host", cfg.ml_rl_service.host, "--port", str(cfg.ml_rl_service.port)],
            cwd=str(REPO_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        time.sleep(8)

        controller_log = open(trial_dir / "controller.log", "w")
        controller = subprocess.Popen(
            [str(VENV_CTRL / "bin" / "python3.9"), "-u", "-m", "ryu.cmd.manager", "--observe-links",
             "controller.main_app", "controller.telemetry_collector"],
            cwd=str(REPO_ROOT), env=env, stdout=controller_log, stderr=subprocess.STDOUT,
        )
        time.sleep(10)

        spec = load_topology_spec(cfg.network.topology_name)
        net = build_network(spec, "127.0.0.1", cfg.controller.of_listen_port, fail_mode="secure")
        net.start()
        # ARP first, then load: iperf3 clients stall on unresolved hosts,
        # and traffic started before discovery converges floods the
        # control plane while the controller is still learning.
        net.pingAll(timeout="1")
        bg_procs = _start_background_traffic(
            net, SETTLE_BEFORE_FAULT_S + FAULT_DURATION_S + POST_FAULT_OBSERVE_S + 120,
            mbit=background_mbit,
        )
        log(f"  network up, background traffic running; settling {SETTLE_BEFORE_FAULT_S}s")
        time.sleep(SETTLE_BEFORE_FAULT_S)

        with Database(db_path) as db:
            initial = db.query_one(
                "SELECT new_path FROM route_changes WHERE changed = 1 ORDER BY timestamp DESC LIMIT 1"
            )
        active_path = initial["new_path"] if initial else None
        if not active_path:
            raise RuntimeError("controller never installed an initial path")
        log(f"  active path {active_path}")

        baseline = _measure_flow(net, IPERF_SAMPLE_S, cfg.traffic.iperf3_port)
        log(f"  baseline: {baseline}")

        switches = active_path.split(",")
        sw_a, sw_b = switches[0], switches[1]
        injector = FailureInjector(net, spec, ground_truth_path=str(trial_dir / "faults.jsonl"))

        import threading
        fault_start = time.time()
        fault_thread = threading.Thread(
            target=injector.inject, args=(sw_a, sw_b, fault_type, FAULT_DURATION_S), daemon=True
        )
        fault_thread.start()
        log(f"  injected {fault_type} on {sw_a}-{sw_b}")

        service_killed_at = None
        if kill_service_mid_run and service.poll() is None:
            time.sleep(20)
            log("  killing ml_rl_service mid-run (E8 safety-gate test)")
            service.terminate()
            service_killed_at = time.time()

        time.sleep(40)
        during = _measure_flow(net, IPERF_SAMPLE_S, cfg.traffic.iperf3_port)
        log(f"  during fault: {during}")

        # Let the fault run its full course and JOIN the injector thread
        # before touching the network again. The pilot tore down while an
        # injector was still sleeping; it then woke into a dead Mininet
        # and crashed inside recover() ('NoneType' has no attribute
        # 'node'), leaving the previous trial's fault notionally
        # un-recovered as the next trial started.
        remaining = max(0.0, FAULT_DURATION_S - (time.time() - fault_start))
        if remaining:
            log(f"  waiting {remaining:.0f}s for the fault to complete and recover")
            time.sleep(remaining)
        fault_thread.join(timeout=60)
        if fault_thread.is_alive():
            log("  WARNING: injector thread did not finish; recording it")
            record["injector_hung"] = True

        after = _measure_flow(net, IPERF_SAMPLE_S, cfg.traffic.iperf3_port)

        timeline = _timeline_from_db(db_path, fault_start, service_killed_at)
        log(f"  timeline: detected={timeline['detected']} "
            f"recovery_time_s={timeline['recovery_time_s']} "
            f"reroutes={timeline['reroute_count']}")

        record.update({
            "baseline": baseline, "during_fault": during, "after": after,
            "fault_link": f"{sw_a}-{sw_b}", "initial_path": active_path,
            **timeline,
        })
    except Exception as exc:  # a failed trial is recorded, not silently skipped
        log(f"  TRIAL ERROR: {exc}")
        record["error"] = str(exc)
    finally:
        for proc in bg_procs:
            try:
                proc.kill()
            except Exception:
                pass
        if net is not None:
            try:
                net.stop()
            except Exception:
                pass
        for proc in (controller, service):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    finished_at = time.time()
    record["duration_s"] = finished_at - started_at

    # Record into the SHARED results database, not the trial's own.
    shared_db = REPO_ROOT / "results" / "experiments.db"
    shared_db.parent.mkdir(parents=True, exist_ok=True)
    with Database(shared_db) as db:
        db.insert("experiment_runs", {
            "experiment_id": experiment_id,
            "seed": seed,
            "system": system,
            "started_at": started_at,
            "finished_at": finished_at,
            "results_json": json.dumps(record),
            "data_source": "emulated_mininet",
        })
    (trial_dir / "result.json").write_text(json.dumps(record, indent=2))
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", help="e.g. E1")
    parser.add_argument("--seeds", type=int, nargs="+")
    parser.add_argument("--systems", nargs="+", help="override the experiment's systems")
    parser.add_argument("--faults", nargs="+", help="override the experiment's faults")
    parser.add_argument("--pilot", action="store_true",
                        help="smallest useful check: E1, B1 vs P2, one seed")
    parser.add_argument("--background-mbit", type=int, default=None,
                        help="E7's load sweep: offered background load per host pair, "
                             "in Mbit/s (default: the runner's standard 2)")
    parser.add_argument("--ablate-gates", action="store_true",
                        help="E5's second arm: persistence.consecutive=1 and the "
                             "stability cooldown/hold/min-improvement zeroed")
    args = parser.parse_args()

    results_root = REPO_ROOT / "results" / "raw" / "experiments"
    results_root.mkdir(parents=True, exist_ok=True)

    if args.pilot:
        combos = [("E1", "B1_SHORTEST_PATH", "gradual_degradation", 1),
                  ("E1", "P2_ML_RL", "gradual_degradation", 1)]
    else:
        if not args.experiment:
            parser.error("--experiment is required unless --pilot")
        exp = by_id(args.experiment)
        systems = args.systems or list(exp.systems)
        faults = args.faults or list(exp.faults)
        seeds = args.seeds or list(range(1, exp.seeds_required + 1))
        combos = [(exp.experiment_id, s, f, seed)
                  for s in systems for f in faults for seed in seeds]

    log(f"{len(combos)} trial(s) queued")
    records = []
    for experiment_id, system, fault, seed in combos:
        records.append(run_trial(experiment_id, system, fault, seed, results_root,
                                 kill_service_mid_run=(experiment_id == "E8"),
                                 ablate_gates=args.ablate_gates,
                                 background_mbit=args.background_mbit))

    print()
    log("SUMMARY")
    print(f"{'trial':<45}{'detected':>9}{'recov_s':>9}{'reroutes':>9}{'thr_during':>12}")
    for r in records:
        if r.get("error"):
            print(f"{r['trial_id']:<45}   ERROR: {r['error'][:40]}")
            continue
        thr = r["during_fault"]["throughput_bps"]
        thr_s = f"{thr/1e6:.2f}M" if thr else "n/a"
        rec = r["recovery_time_s"]
        print(f"{r['trial_id']:<45}{str(r['detected']):>9}"
              f"{(f'{rec:.1f}' if rec else 'n/a'):>9}{r['reroute_count']:>9}{thr_s:>12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
