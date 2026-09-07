#!/usr/bin/env python3
"""Recomputes labelled feature vectors from ALREADY-COLLECTED telemetry.

Re-running Mininet to change the feature set would be wasteful and would
also change the data underneath -- a new run has different traffic
timing, so a model trained on it could not be compared like-for-like
with one trained on the old features. Re-extraction keeps the raw
telemetry and fault ground truth fixed and varies only
common/features.py, which is the only honest way to attribute a change
in model performance to the feature change itself.

Same approach Phase 9 used when a missing latency join was fixed.

Run inside the VM under system python3 (Mininet not required, but the
telemetry database lives there):
    sudo python3 ml/dataset/reextract_features.py \
        --telemetry-db results/raw/dataset_matched/telemetry.db \
        --ground-truth-dir results/raw/dataset_matched \
        --output-dir results/raw/dataset_matched \
        --seeds 1 2 3 4 5 6 7 8
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.config_loader import load_config  # noqa: E402
from ml.dataset.generate_dataset import _extract_labelled_windows  # noqa: E402
from network.topo_builder import load_topology_spec  # noqa: E402


def _run_bounds_and_faults(gt_path: Path) -> tuple[float, float, list[dict]]:
    """Run boundaries are taken from the ground-truth log's own extent,
    padded so the first fault's anticipation window and the last fault's
    recovery tail are both inside the range."""
    events = [json.loads(line) for line in gt_path.read_text().splitlines() if line.strip()]
    if not events:
        raise ValueError(f"{gt_path} is empty")
    stamps = [e["logged_at"] for e in events]
    return min(stamps) - 60.0, max(stamps) + 60.0, events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telemetry-db", required=True)
    parser.add_argument("--ground-truth-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--seeds", type=int, nargs="+", required=True)
    parser.add_argument("--topo", default=None)
    args = parser.parse_args()

    config = load_config()
    spec = load_topology_spec(args.topo or config.network.topology_name)
    links = [tuple(link["endpoints"]) for link in spec["links"]]
    link_capacities = {
        "-".join(sorted(link["endpoints"])): link["bw"] * 1_000_000
        for link in spec["links"] if "bw" in link
    }

    db_path = Path(args.telemetry_db)
    gt_dir = Path(args.ground_truth_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    totals = {"windows": 0, "positive": 0, "negative": 0}
    per_run = []
    for seed in args.seeds:
        gt_path = gt_dir / f"ground_truth_seed{seed}.jsonl"
        if not gt_path.exists():
            print(f"seed {seed}: no ground truth at {gt_path}, skipping", flush=True)
            continue
        start_ts, end_ts, events = _run_bounds_and_faults(gt_path)
        windows, positive, negative = _extract_labelled_windows(
            db_path, links, start_ts, end_ts, events, config, seed, out_dir, link_capacities,
        )
        frac = positive / windows if windows else 0.0
        print(f"seed {seed}: {windows} windows, {positive} positive ({frac*100:.2f}%)", flush=True)
        per_run.append({"seed": seed, "windows": windows, "positive": positive, "negative": negative})
        totals["windows"] += windows
        totals["positive"] += positive
        totals["negative"] += negative

    summary = {
        "topo": args.topo or config.network.topology_name,
        "num_runs": len(per_run),
        "per_run": per_run,
        "total_windows": totals["windows"],
        "total_positive": totals["positive"],
        "total_negative": totals["negative"],
        "positive_fraction": (totals["positive"] / totals["windows"]) if totals["windows"] else 0.0,
        "data_source": "emulated_mininet",
        "note": (
            "Re-extracted from stored telemetry with the scale-invariant "
            "feature set; raw telemetry and fault ground truth unchanged."
        ),
    }
    (REPO_ROOT / "results" / "tables").mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "results" / "tables" / "dataset_matched_summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    print(f"\ntotal: {totals['windows']} windows, {totals['positive']} positive "
          f"({summary['positive_fraction']*100:.2f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
