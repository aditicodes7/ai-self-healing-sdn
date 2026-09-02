#!/usr/bin/env python3
"""Reloads the saved degradation model (models/degradation_model.joblib +
models/metadata.json) and independently re-evaluates it against the held
-out test runs, printing the same metrics ml/train_model.py reported.

This exists as an independent reproducibility check (Phase 28): if this
script's output doesn't match results/tables/ml_metrics.json, something
about the saved artefact or the dataset has drifted.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import joblib

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ml.train_model import evaluate, load_dataset, to_matrix  # noqa: E402


def main() -> int:
    models_dir = REPO_ROOT / "models"
    metadata = json.loads((models_dir / "metadata.json").read_text())
    model = joblib.load(models_dir / "degradation_model.joblib")

    dataset_dir = REPO_ROOT / "results" / "raw" / "dataset"
    test_rows = load_dataset(dataset_dir, tuple(metadata["test_seeds"]))

    feature_order = metadata["feature_order"]
    for row in test_rows:
        missing = set(feature_order) - set(row["features"].keys())
        if missing:
            raise ValueError(f"test row missing features the saved model expects: {missing}")

    X_test, y_test = to_matrix(test_rows, feature_order)
    res = evaluate(model, X_test, y_test, threshold=metadata["threshold"])

    print(f"model: {metadata['chosen_model']} (sklearn {metadata['sklearn_version']}, "
          f"trained from git commit {metadata['git_commit']})")
    print(f"test seeds: {metadata['test_seeds']}, n={res['n_test_samples']}, "
          f"positive={res['n_test_positive']}")
    print(f"threshold={res['threshold']}")
    print(f"precision={res['precision']:.4f} recall={res['recall']:.4f} f1={res['f1']:.4f}")
    print(f"roc_auc={res['roc_auc']} pr_auc={res['pr_auc']:.4f}")
    print(f"confusion_matrix={res['confusion_matrix']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
