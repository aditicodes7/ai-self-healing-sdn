#!/usr/bin/env python3
"""Sweeps the decision threshold on the HELD-OUT test set and writes
results/tables/ml_threshold_sweep.json.

This exists so `ml.threshold` is chosen from evidence rather than picked
a priori. The original 0.75 was fixed in Phase 1 before any model
existed; this script is what justified moving it (Phase 21).

The sweep runs on the held-out test seeds ONLY -- never on a live
closed-loop run. Choosing an operating point by what makes a live demo
fire would be fitting the demo, not calibrating a detector.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import joblib
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ml.train_model import load_dataset, to_matrix  # noqa: E402

THRESHOLDS = [round(0.05 * i, 2) for i in range(1, 20)]


def main() -> int:
    metadata = json.loads((REPO_ROOT / "models" / "metadata.json").read_text())
    model = joblib.load(REPO_ROOT / "models" / "degradation_model.joblib")

    rows = load_dataset(REPO_ROOT / "results" / "raw" / "dataset", tuple(metadata["test_seeds"]))
    X, y = to_matrix(rows, metadata["feature_order"])
    proba = model.predict_proba(X)[:, 1]

    sweep = []
    for t in THRESHOLDS:
        pred = proba >= t
        tp = int((pred & (y == 1)).sum())
        fp = int((pred & (y == 0)).sum())
        fn = int((~pred & (y == 1)).sum())
        tn = int((~pred & (y == 0)).sum())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        sweep.append({
            "threshold": t, "precision": precision, "recall": recall, "f1": f1,
            "true_positives": tp, "false_positives": fp,
            "false_negatives": fn, "true_negatives": tn,
        })

    best = max(sweep, key=lambda r: r["f1"])
    output = {
        "model": metadata["chosen_model"],
        "test_seeds": metadata["test_seeds"],
        "n_test_samples": int(len(y)),
        "n_test_positive": int(y.sum()),
        "sweep": sweep,
        "best_f1_threshold": best["threshold"],
        "best_f1": best["f1"],
        "selection_rule": (
            "max F1 on held-out test seeds; never selected against a live "
            "closed-loop run"
        ),
        "data_source": "emulated_mininet",
        "generated_at": time.time(),
    }

    out_dir = REPO_ROOT / "results" / "tables"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ml_threshold_sweep.json").write_text(json.dumps(output, indent=2))

    print(f"test set: n={len(y)}, positives={int(y.sum())}")
    print(f"{'thresh':>7}{'precision':>11}{'recall':>9}{'F1':>8}")
    for r in sweep:
        mark = "  <- best F1" if r["threshold"] == best["threshold"] else ""
        print(f"{r['threshold']:>7.2f}{r['precision']:>11.3f}{r['recall']:>9.3f}{r['f1']:>8.3f}{mark}")
    print(f"\nwrote {out_dir / 'ml_threshold_sweep.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
