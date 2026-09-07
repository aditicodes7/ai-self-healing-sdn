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
FAULT_DURATION_S = 150
POST_FAULT_OBSERVE_S = 30
IPERF_SAMPLE_S = 10


def log(msg: str) -> None:
    print(f"[exp {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _write_trial_config(trial_dir: Path, system: str, db_path: Path) -> Path:
    """Materialise the exact config this trial ran under, so a result can
    always be traced back to the settings that produced it."""
    base = yaml.safe_load((REPO_ROOT / "config" / "config.yaml").read_text())
    base["experiment"]["system"] = system
    base["database"]["path"] = str(db_path)
    out = trial_dir / "config.yaml"
    out.write_text(yaml.safe_dump(base, sort_keys=False))
    return out


def _measure_flow(net, duration_s: int, port: int) -> dict:
    """One iperf3 h1->h6 sample plus a ping sample. Returns nulls for
    anything that failed rather than substituting a number."""
    h1, h6 = net.get("h1"), net.get("h6")
    result: dict = {"throughput_bps": None, "loss_percent": None, "rtt_avg_ms": None}

    server = h6.popen(["iperf3", "-s", "-p", str(port), "-1", "-J"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    try:
        out = h1.cmd(f"iperf3 -c {h6.IP()} -p {port} -t {duration_s} -J")
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
        ping_out = h1.cmd(f"ping -c 10 -i 0.2 -W 1 {h6.IP()}")
        for line in ping_out.splitlines():
            if "packet loss" in line:
                result["loss_percent"] = float(line.split("%")[0].split()[-1])
            if line.startswith("rtt") or line.startswith("round-trip"):
                result["rtt_avg_ms"] = float(line.split("=")[1].split("/")[1])
    except (ValueError, IndexError):
        pass

    return result


def _timeline_from_db(db_path: Path, fault_start: float) -> dict:
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
        "final_path": reroutes[-1]["new_path"] if reroutes else None,
        "n_predictions_during_fault": n_predictions,
    }


def run_trial(experiment_id: str, system: str, fault_type: str, seed: int,
              results_root: Path, kill_service_mid_run: bool = False) -> dict:
    cfg = load_config()
    trial_id = f"{experiment_id}_{system}_{fault_type}_seed{seed}"
    trial_dir = results_root / trial_id
    if trial_dir.exists():
        shutil.rmtree(trial_dir)
    trial_dir.mkdir(parents=True)

    db_path = trial_dir / "network.db"
    trial_config = _write_trial_config(trial_dir, system, db_path)

    log(f"=== {trial_id} ===")
    started_at = time.time()
    service = controller = net = None
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
        log(f"  network up; settling {SETTLE_BEFORE_FAULT_S}s")
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
        threading.Thread(
            target=injector.inject, args=(sw_a, sw_b, fault_type, FAULT_DURATION_S), daemon=True
        ).start()
        log(f"  injected {fault_type} on {sw_a}-{sw_b}")

        if kill_service_mid_run and service.poll() is None:
            time.sleep(20)
            log("  killing ml_rl_service mid-run (E8 safety-gate test)")
            service.terminate()

        time.sleep(40)
        during = _measure_flow(net, IPERF_SAMPLE_S, cfg.traffic.iperf3_port)
        log(f"  during fault: {during}")

        remaining = max(0.0, FAULT_DURATION_S - (time.time() - fault_start))
        time.sleep(min(remaining, 60))
        after = _measure_flow(net, IPERF_SAMPLE_S, cfg.traffic.iperf3_port)

        timeline = _timeline_from_db(db_path, fault_start)
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
                                 kill_service_mid_run=(experiment_id == "E8")))

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
