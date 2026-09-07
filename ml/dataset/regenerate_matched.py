#!/usr/bin/env python3
"""Regenerates the training dataset under DEPLOYMENT-MATCHED conditions.

Why this exists
---------------
Phase 8 generated the dataset with a controller running alone: no
inference service, no measurement load. That produced a median probe
latency of 2.6 ms. Phase 22's experiment harness runs the same
controller alongside the ML/RL service and iperf3/ping measurement, and
measures 40.9 ms before any fault -- roughly 16x outside anything in the
training data. Since Phase 9's permutation importance ranks latency
features first, the model was being asked to extrapolate on inputs it
had never seen, and its output on a badly degraded link (max P=0.290,
latency 40.9 -> 342 ms) ranked BELOW a healthy one (0.720).

That is train/serve skew, and the fix is to train under the conditions
the system actually runs in rather than to keep retuning thresholds
against a distribution the model never learned.

What this changes vs Phase 8
----------------------------
  * the ML/RL inference service runs throughout, so the control plane
    carries the same load it carries in deployment
  * the controller runs as B1_SHORTEST_PATH: it collects telemetry and
    issues predictions (which is what generates the load) but never
    remediates, so a fault's effect is never cut short by the system
    healing it -- remediation would confound the labels
  * everything else -- topology, traffic, fault schedule, seeds, label
    rules -- is unchanged, so the comparison against the Phase 8 model
    is like-for-like

Run inside the VM under system python3:
    sudo python3 ml/dataset/regenerate_matched.py --seeds 1 2 3 4 5 6 7 8
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from common.config_loader import load_config  # noqa: E402

VENV_ML = Path(os.environ.get("VENV_ML", REPO_ROOT / "venv-ml"))
VENV_CTRL = Path(os.environ.get("VENV_CTRL", Path.home() / "venv-ctrl-ai-self-healing-sdn"))


def log(msg: str) -> None:
    print(f"[regen {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5, 6, 7, 8])
    parser.add_argument("--duration", type=float, default=600.0)
    args = parser.parse_args()

    cfg = load_config()
    work = REPO_ROOT / "results" / "raw" / "dataset_matched"
    work.mkdir(parents=True, exist_ok=True)

    # A config that keeps the controller passive but fully loaded.
    base = yaml.safe_load((REPO_ROOT / "config" / "config.yaml").read_text())
    base["experiment"]["system"] = "B1_SHORTEST_PATH"
    base["database"]["path"] = str(work / "telemetry.db")
    trial_config = work / "config.yaml"
    trial_config.write_text(yaml.safe_dump(base, sort_keys=False))
    env = dict(os.environ, AI_SDN_CONFIG=str(trial_config))

    service = controller = None
    try:
        log("starting ml_rl_service (load parity with deployment)…")
        service = subprocess.Popen(
            [str(VENV_ML / "bin" / "python3"), "-m", "uvicorn", "service.ml_rl_service:app",
             "--host", cfg.ml_rl_service.host, "--port", str(cfg.ml_rl_service.port)],
            cwd=str(REPO_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        time.sleep(8)

        log("starting controller as B1_SHORTEST_PATH (predicts, never remediates)…")
        controller_log = open(work / "controller.log", "w")
        controller = subprocess.Popen(
            [str(VENV_CTRL / "bin" / "python3.9"), "-u", "-m", "ryu.cmd.manager", "--observe-links",
             "controller.main_app", "controller.telemetry_collector"],
            cwd=str(REPO_ROOT), env=env, stdout=controller_log, stderr=subprocess.STDOUT,
        )
        time.sleep(10)
        if controller.poll() is not None:
            log("FAIL: controller exited immediately")
            return 1

        log(f"generating {len(args.seeds)} runs x {args.duration:.0f}s "
            f"(~{len(args.seeds) * args.duration / 60:.0f} min)…")
        result = subprocess.run(
            [sys.executable, "ml/dataset/generate_dataset.py",
             "--seeds", *[str(s) for s in args.seeds],
             "--duration", str(args.duration),
             "--output-dir", str(work)],
            cwd=str(REPO_ROOT), env=env,
        )
        return result.returncode
    finally:
        for proc in (controller, service):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
