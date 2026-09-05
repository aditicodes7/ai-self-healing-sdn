#!/usr/bin/env python3
"""Phase 21 acceptance gate: does the WHOLE loop actually work against a
live network?

Everything Phases 5-20 built runs here at once, for real:
  real Mininet + OVS switches
  -> real Ryu controller with telemetry, ML inference and the healing loop
  -> a real injected fault on a link the managed flow is actually using
  -> and then we check the database for what the system did about it.

Self-contained by design: it starts the ML/RL service and the controller
itself, builds the network, injects the fault, polls the database, and
tears everything down from inside ONE process. Phase 13 was debugged the
hard way after `multipass exec` timing races made separate follow-up
commands race against network teardown -- never split this across
several shell invocations again.

Run inside the VM, from the repo root, under the SYSTEM python3 -- not
venv-ctrl. Mininet is an apt package bound to the system interpreter and
is not importable from venv-ctrl's 3.9; this script only needs Mininet
plus the pure-python modules, and launches the controller and inference
service as subprocesses under their own interpreters:

    sudo python3 tests/e2e/run_closed_loop_e2e.py

Requires (in the VM): python3-networkx and python3-yaml via apt.

Exits 0 only if the loop genuinely detected, rerouted, and verified.
Every check prints what it actually observed, not a summary judgement.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.config_loader import load_config  # noqa: E402
from database.db import Database  # noqa: E402
from network.failure_injector import FailureInjector  # noqa: E402
from network.topo_builder import build_network, load_topology_spec  # noqa: E402

VENV_ML = Path(os.environ.get("VENV_ML", REPO_ROOT / "venv-ml"))
VENV_CTRL = Path(os.environ.get("VENV_CTRL", Path.home() / "venv-ctrl-ai-self-healing-sdn"))

CONVERGENCE_TIMEOUT_S = 90
REACTION_TIMEOUT_S = 120
FAULT_DURATION_S = 90


def log(msg: str) -> None:
    print(f"[e2e {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_for(description: str, predicate, timeout_s: float, interval_s: float = 2.0):
    """Polls until predicate() returns something truthy. Returns that
    value, or None on timeout -- callers report the timeout honestly
    rather than treating it as a pass."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            value = predicate()
        except Exception as exc:  # a not-yet-created table, etc.
            value = None
            log(f"  (waiting on {description}: {exc})")
        if value:
            return value
        time.sleep(interval_s)
    return None


def main() -> int:
    cfg = load_config()
    db_path = REPO_ROOT / cfg.database.path

    # Start from a clean database so every row we find is from THIS run.
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    log(f"cleared database at {db_path}")

    service = None
    controller = None
    net = None
    failures: list[str] = []

    try:
        # -- 1. ML/RL inference service ---------------------------------
        log("starting ml_rl_service…")
        service = subprocess.Popen(
            [str(VENV_ML / "bin" / "python3"), "-m", "uvicorn", "service.ml_rl_service:app",
             "--host", cfg.ml_rl_service.host, "--port", str(cfg.ml_rl_service.port)],
            cwd=str(REPO_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        time.sleep(8)
        if service.poll() is not None:
            log("FAIL: ml_rl_service exited immediately")
            return 1

        # -- 2. Ryu controller with the closed loop ----------------------
        log("starting ryu-manager (main_app + telemetry_collector + healing loop)…")
        controller_log = open("/tmp/e2e_controller.log", "w")
        controller = subprocess.Popen(
            # -u matters: stdout redirected to a file is block-buffered,
            # so terminating the controller at teardown discards whatever
            # is still in the buffer -- which on the first live run threw
            # away the exact logs needed to diagnose the failure.
            [str(VENV_CTRL / "bin" / "python3.9"), "-u", "-m", "ryu.cmd.manager", "--observe-links",
             "controller.main_app", "controller.telemetry_collector"],
            cwd=str(REPO_ROOT), stdout=controller_log, stderr=subprocess.STDOUT,
        )
        time.sleep(10)
        if controller.poll() is not None:
            log("FAIL: ryu-manager exited immediately — see /tmp/e2e_controller.log")
            return 1

        # -- 3. the network ---------------------------------------------
        topo_spec = load_topology_spec(cfg.network.topology_name)
        log(f"building topology {cfg.network.topology_name}…")
        net = build_network(
            topo_spec,
            controller_ip="127.0.0.1",
            controller_port=cfg.controller.of_listen_port,
            fail_mode="secure",
        )
        # build_network only CONSTRUCTS the topology; without start() the
        # switches never come up and never dial the controller, which
        # looks exactly like "the controller is broken" from the outside.
        net.start()
        log("network started; waiting for LLDP discovery and initial path install…")

        # -- 4. wait for the loop to take ownership of the flow ----------
        def initial_path_row():
            with Database(db_path) as db:
                return db.query_one(
                    "SELECT * FROM remediation_events WHERE event_type = 'initial_path_installed' LIMIT 1"
                )

        row = wait_for("initial path install", initial_path_row, CONVERGENCE_TIMEOUT_S)
        if row is None:
            failures.append("the controller never installed an initial path for h1-h6")
            log("FAIL: no initial_path_installed event appeared")
            return _finish(failures)
        log(f"  initial path installed: {row['detail_json']}")

        with Database(db_path) as db:
            change = db.query_one(
                "SELECT new_path FROM route_changes WHERE changed = 1 ORDER BY timestamp DESC LIMIT 1"
            )
        active_path = change["new_path"] if change else None
        log(f"  active path is {active_path}")
        if not active_path:
            failures.append("no installed path recorded in route_changes")
            return _finish(failures)

        # -- 5. break a link the flow is actually using ------------------
        switches = active_path.split(",")
        sw_a, sw_b = switches[0], switches[1]
        log(f"injecting 'gradual_degradation' on {sw_a}-{sw_b} (on the active path)…")
        injector = FailureInjector(net, topo_spec, ground_truth_path="/tmp/e2e_faults.jsonl")

        import threading

        fault_thread = threading.Thread(
            target=injector.inject,
            args=(sw_a, sw_b, "gradual_degradation", FAULT_DURATION_S),
            daemon=True,
        )
        fault_thread.start()

        # -- 6. did the system notice, act, and check its own work? ------
        def predictions_seen():
            with Database(db_path) as db:
                r = db.query_one("SELECT COUNT(*) c FROM predictions")
                return r["c"] if r and r["c"] else None

        n_preds = wait_for("degradation predictions", predictions_seen, REACTION_TIMEOUT_S)
        log(f"  predictions written: {n_preds}")
        if not n_preds:
            failures.append("no predictions were ever written (ML service or telemetry not working)")

        def incident_row():
            with Database(db_path) as db:
                return db.query_one(
                    "SELECT * FROM incidents WHERE incident_id LIKE 'inc-%' ORDER BY opened_at DESC LIMIT 1"
                )

        incident = wait_for("an incident to open", incident_row, REACTION_TIMEOUT_S)
        if incident is None:
            failures.append("the loop never opened an incident for the degrading link")
            log("FAIL: no incident opened")
            return _finish(failures)
        log(f"  incident {incident['incident_id']} opened on {incident['link_id']}: {incident['reason']}")

        def reroute_row():
            with Database(db_path) as db:
                return db.query_one(
                    "SELECT * FROM route_changes WHERE incident_id = ? AND changed = 1",
                    (incident["incident_id"],),
                )

        reroute = wait_for("a reroute", reroute_row, REACTION_TIMEOUT_S)
        if reroute is None:
            failures.append("an incident opened but no reroute was installed")
        else:
            log(f"  rerouted {reroute['old_path']} -> {reroute['new_path']} "
                f"(selector={reroute['selector']}, rl_fallback={reroute['rl_fallback']})")
            if reroute["new_path"] == reroute["old_path"]:
                failures.append("the 'reroute' selected the same path it was already on")

        def verification_row():
            with Database(db_path) as db:
                return db.query_one(
                    "SELECT * FROM verification_results WHERE incident_id = ?",
                    (incident["incident_id"],),
                )

        verification = wait_for("verification to complete", verification_row, REACTION_TIMEOUT_S)
        if verification is None:
            failures.append("the reroute was never verified")
        else:
            log(f"  verification: {verification['classification']} "
                f"(retries={verification['retry_count']}) on {verification['path']}")

        with Database(db_path) as db:
            final = db.query_one(
                "SELECT status FROM incidents WHERE incident_id = ?", (incident["incident_id"],)
            )
            events = db.query(
                "SELECT event_type FROM remediation_events WHERE incident_id = ? ORDER BY id",
                (incident["incident_id"],),
            )
        log(f"  incident closed as: {final['status'] if final else 'STILL OPEN'}")
        log(f"  events emitted: {[e['event_type'] for e in events]}")

        return _finish(failures)

    finally:
        log("tearing down…")
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


def _finish(failures: list[str]) -> int:
    print()
    if failures:
        log("RESULT: FAIL")
        for f in failures:
            log(f"  - {f}")
        return 1
    log("RESULT: PASS — the closed loop detected, rerouted, and verified against a live network")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
