#!/usr/bin/env python3
"""Isolates WHY live latency-probe measurements are ~200x larger than the
values the ML model was trained on (2.58 ms median in Phase 8's dataset
vs ~450 ms live in Phase 21's first end-to-end runs).

Runs the identical controller + topology twice and compares the latency
the probe reports:

  A. inference DISABLED -- ml_rl_service is not started, so
     InferenceClient's HTTP POST fails fast and no prediction work
     happens inside the controller's event loop. This is the condition
     Phase 8's dataset generation ran under.
  B. inference ENABLED -- ml_rl_service running, one blocking HTTP
     inference call per link per telemetry cycle, which is the condition
     the live closed loop runs under.

If B is dramatically worse than A, the controller's own inference calls
are inflating the very latency measurements the model consumes -- a
feedback pathology, not a property of the network being measured.

Run inside the VM under system python3:
    sudo python3 tests/e2e/measure_probe_overhead.py
"""
from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.config_loader import load_config  # noqa: E402
from database.db import Database  # noqa: E402
from network.topo_builder import build_network, load_topology_spec  # noqa: E402

VENV_ML = Path(os.environ.get("VENV_ML", REPO_ROOT / "venv-ml"))
VENV_CTRL = Path(os.environ.get("VENV_CTRL", Path.home() / "venv-ctrl-ai-self-healing-sdn"))

SETTLE_S = 60


def log(msg: str) -> None:
    print(f"[probe {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run_condition(name: str, with_inference: bool, cfg) -> dict:
    db_path = REPO_ROOT / cfg.database.path
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    service = None
    controller = None
    net = None
    try:
        if with_inference:
            log(f"[{name}] starting ml_rl_service…")
            service = subprocess.Popen(
                [str(VENV_ML / "bin" / "python3"), "-m", "uvicorn", "service.ml_rl_service:app",
                 "--host", cfg.ml_rl_service.host, "--port", str(cfg.ml_rl_service.port)],
                cwd=str(REPO_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            )
            time.sleep(8)
        else:
            log(f"[{name}] ml_rl_service deliberately NOT started")

        log(f"[{name}] starting controller…")
        controller = subprocess.Popen(
            [str(VENV_CTRL / "bin" / "python3.9"), "-u", "-m", "ryu.cmd.manager", "--observe-links",
             "controller.main_app", "controller.telemetry_collector"],
            cwd=str(REPO_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        time.sleep(10)

        spec = load_topology_spec(cfg.network.topology_name)
        net = build_network(spec, "127.0.0.1", cfg.controller.of_listen_port, fail_mode="secure")
        net.start()
        log(f"[{name}] network up; collecting for {SETTLE_S}s…")
        time.sleep(SETTLE_S)

        with Database(db_path) as db:
            rows = db.query("SELECT latency_ms FROM features WHERE latency_ms IS NOT NULL")
            n_pred = db.query_one("SELECT COUNT(*) c FROM predictions")["c"]
        values = sorted(r["latency_ms"] for r in rows)
        if not values:
            return {"name": name, "n": 0, "predictions": n_pred}
        return {
            "name": name,
            "n": len(values),
            "predictions": n_pred,
            "median": statistics.median(values),
            "mean": statistics.mean(values),
            "p95": values[int(len(values) * 0.95)],
            "max": values[-1],
        }
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


def main() -> int:
    cfg = load_config()
    results = []
    for name, with_inference in (("inference OFF", False), ("inference ON", True)):
        results.append(run_condition(name, with_inference, cfg))
        time.sleep(5)

    print()
    print("=" * 68)
    print("latency-probe measurements, identical network and controller")
    print("=" * 68)
    print(f"{'condition':<16}{'n':>6}{'preds':>7}{'median':>10}{'mean':>10}{'p95':>10}{'max':>10}")
    for r in results:
        if not r.get("n"):
            print(f"{r['name']:<16}{0:>6}{r.get('predictions', 0):>7}   (no samples)")
            continue
        print(f"{r['name']:<16}{r['n']:>6}{r['predictions']:>7}"
              f"{r['median']:>9.1f}{r['mean']:>10.1f}{r['p95']:>10.1f}{r['max']:>10.1f}")
    print()
    print("Phase 8 training data (what the model learned from):")
    print(f"{'dataset':<16}{'':>6}{'':>7}{2.58:>9.1f}{'':>10}{32.8:>10.1f}{69.3:>10.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
