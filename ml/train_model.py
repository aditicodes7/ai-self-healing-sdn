#!/usr/bin/env python3
"""Trains and evaluates the degradation-prediction model on the Phase 8
dataset (results/raw/dataset/run_<seed>.jsonl).

SPLIT BY RUN, NOT BY ROW: seeds 1-6 are train, seeds 7-8 are held out as
test. Rows within one run are temporally correlated (a single fault's
effect spans many consecutive windows); a row-wise split would leak that
correlation across train/test and inflate reported metrics. This is a
fixed, documented split -- not re-randomized per invocation -- so results
are reproducible.

Trains all three approved candidates (scikit-learn only; NO XGBoost) and
picks the best by PR-AUC: with a ~1.94% positive class (see Phase 8's
results/tables/dataset_summary.json), PR-AUC is far more informative than
ROC-AUC, which stays high even for a poor classifier under this much
imbalance.

Class imbalance: sample_weight='balanced' at fit time, applied uniformly
across all three model types -- not oversampling/undersampling, which
would change the data itself. NaN handling:
HistGradientBoostingClassifier supports NaN features natively (matches
common/features.py's explicit "None means not yet computable" policy with
no imputation at all). RandomForestClassifier and LogisticRegression
cannot accept NaN, so those two run through a Pipeline with median
imputation (sklearn.impute.SimpleImputer) -- a documented, visible choice
made HERE, not a silent fillna(0) inside common/features.py itself.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.calibration import calibration_curve
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    precision_recall_fscore_support,
    roc_auc_score,
    roc_curve,
    precision_recall_curve,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.utils.class_weight import compute_sample_weight

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.config_loader import load_config  # noqa: E402
from common.schema import DataSource  # noqa: E402

TRAIN_SEEDS = (1, 2, 3, 4, 5, 6)
TEST_SEEDS = (7, 8)
RANDOM_STATE = 42


def load_dataset(dataset_dir: Path, seeds: tuple[int, ...]) -> list[dict]:
    rows = []
    for seed in seeds:
        path = dataset_dir / f"run_{seed}.jsonl"
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found -- run ml/dataset/generate_dataset.py first "
                f"(or, if it was generated in the VM, copy results/raw/dataset/ over)"
            )
        with open(path) as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def to_matrix(rows: list[dict], feature_order: list[str]) -> tuple[np.ndarray, np.ndarray]:
    X = np.full((len(rows), len(feature_order)), np.nan, dtype=float)
    y = np.zeros(len(rows), dtype=int)
    for i, row in enumerate(rows):
        for j, name in enumerate(feature_order):
            v = row["features"].get(name)
            if v is not None:
                X[i, j] = v
        y[i] = row["label"]
    return X, y


def build_models(random_state: int) -> dict:
    return {
        "histgradientboosting": HistGradientBoostingClassifier(random_state=random_state),
        "randomforest": Pipeline(
            [
                ("impute", SimpleImputer(strategy="median")),
                ("clf", RandomForestClassifier(n_estimators=300, random_state=random_state, n_jobs=-1)),
            ]
        ),
        "logisticregression_baseline": Pipeline(
            [
                ("impute", SimpleImputer(strategy="median")),
                ("scale", StandardScaler()),
                ("clf", LogisticRegression(max_iter=2000, random_state=random_state)),
            ]
        ),
    }


def evaluate(model, X_test: np.ndarray, y_test: np.ndarray, threshold: float) -> dict:
    proba = model.predict_proba(X_test)[:, 1]
    y_pred = (proba >= threshold).astype(int)

    precision, recall, f1, _ = precision_recall_fscore_support(
        y_test, y_pred, average="binary", zero_division=0
    )
    roc_auc = roc_auc_score(y_test, proba) if len(np.unique(y_test)) > 1 else None
    pr_auc = average_precision_score(y_test, proba)
    cm = confusion_matrix(y_test, y_pred, labels=[0, 1]).tolist()

    fpr, tpr, _ = roc_curve(y_test, proba)
    pr_p, pr_r, _ = precision_recall_curve(y_test, proba)
    prob_true, prob_pred = calibration_curve(y_test, proba, n_bins=10, strategy="quantile")

    return {
        "threshold": threshold,
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "roc_auc": float(roc_auc) if roc_auc is not None else None,
        "pr_auc": float(pr_auc),
        "confusion_matrix": cm,  # [[TN, FP], [FN, TP]]
        "confusion_matrix_labels": ["negative(0)", "positive(1)"],
        "n_test_samples": int(len(y_test)),
        "n_test_positive": int(y_test.sum()),
        "_roc_curve": {"fpr": fpr.tolist(), "tpr": tpr.tolist()},
        "_pr_curve": {"precision": pr_p.tolist(), "recall": pr_r.tolist()},
        "_calibration_curve": {"prob_true": prob_true.tolist(), "prob_pred": prob_pred.tolist()},
        "_proba": proba,
    }


def make_figures(results: dict, feature_order: list[str], best_name: str, figures_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figures_dir.mkdir(parents=True, exist_ok=True)

    # ROC curves, all models overlaid.
    plt.figure(figsize=(6, 5))
    for name, res in results.items():
        auc = res["roc_auc"]
        label = f"{name} (AUC={auc:.3f})" if auc is not None else name
        plt.plot(res["_roc_curve"]["fpr"], res["_roc_curve"]["tpr"], label=label)
    plt.plot([0, 1], [0, 1], "k--", linewidth=0.8, label="chance")
    plt.xlabel("False Positive Rate")
    plt.ylabel("True Positive Rate")
    plt.title("ROC curve (test runs, seeds 7-8)")
    plt.legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(figures_dir / "ml_roc_curve.png", dpi=150)
    plt.close()

    # PR curves, all models overlaid.
    plt.figure(figsize=(6, 5))
    for name, res in results.items():
        plt.plot(res["_pr_curve"]["recall"], res["_pr_curve"]["precision"],
                  label=f"{name} (AP={res['pr_auc']:.3f})")
    plt.xlabel("Recall")
    plt.ylabel("Precision")
    plt.title("Precision-Recall curve (test runs, seeds 7-8)")
    plt.legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(figures_dir / "ml_pr_curve.png", dpi=150)
    plt.close()

    # Confusion matrix for the chosen model.
    cm = np.array(results[best_name]["confusion_matrix"])
    plt.figure(figsize=(4.5, 4))
    plt.imshow(cm, cmap="Blues")
    for i in range(2):
        for j in range(2):
            plt.text(j, i, str(cm[i, j]), ha="center", va="center")
    plt.xticks([0, 1], ["pred 0", "pred 1"])
    plt.yticks([0, 1], ["true 0", "true 1"])
    plt.title(f"Confusion matrix -- {best_name}\n(threshold={results[best_name]['threshold']})")
    plt.colorbar()
    plt.tight_layout()
    plt.savefig(figures_dir / "ml_confusion_matrix.png", dpi=150)
    plt.close()

    # Calibration curve for the chosen model.
    cal = results[best_name]["_calibration_curve"]
    plt.figure(figsize=(5, 5))
    plt.plot(cal["prob_pred"], cal["prob_true"], "o-", label=best_name)
    plt.plot([0, 1], [0, 1], "k--", linewidth=0.8, label="perfectly calibrated")
    plt.xlabel("Mean predicted probability")
    plt.ylabel("Fraction of positives")
    plt.title(f"Calibration curve -- {best_name}")
    plt.legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(figures_dir / "ml_calibration_curve.png", dpi=150)
    plt.close()

    # Permutation feature importance for the chosen model (top 15).
    importances = results[best_name]["permutation_importance"]
    order = np.argsort(importances["importances_mean"])[::-1][:15]
    names = [feature_order[i] for i in order]
    means = [importances["importances_mean"][i] for i in order]
    stds = [importances["importances_std"][i] for i in order]
    plt.figure(figsize=(7, 6))
    y_pos = np.arange(len(names))
    plt.barh(y_pos, means, xerr=stds)
    plt.yticks(y_pos, names, fontsize=8)
    plt.gca().invert_yaxis()
    plt.xlabel("Permutation importance (PR-AUC drop)")
    plt.title(f"Top 15 permutation feature importances -- {best_name}")
    plt.tight_layout()
    plt.savefig(figures_dir / "ml_feature_importance.png", dpi=150)
    plt.close()


def main() -> int:
    # --dataset-dir keeps the Phase 8 dataset intact alongside Phase 22's
    # deployment-matched one, so the two models stay directly comparable
    # instead of one silently overwriting the other's provenance.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", default=str(REPO_ROOT / "results" / "raw" / "dataset"))
    args = parser.parse_args()

    config = load_config()
    dataset_dir = Path(args.dataset_dir)
    print(f"training from {dataset_dir}")

    train_rows = load_dataset(dataset_dir, TRAIN_SEEDS)
    test_rows = load_dataset(dataset_dir, TEST_SEEDS)

    feature_order = list(train_rows[0]["features"].keys())
    for row in train_rows + test_rows:
        assert list(row["features"].keys()) == feature_order, "feature key order mismatch across rows"

    X_train, y_train = to_matrix(train_rows, feature_order)
    X_test, y_test = to_matrix(test_rows, feature_order)
    sample_weight = compute_sample_weight("balanced", y_train)

    print(f"train: {len(y_train)} rows, {y_train.sum()} positive ({y_train.mean():.4%})")
    print(f"test:  {len(y_test)} rows, {y_test.sum()} positive ({y_test.mean():.4%})")

    # Zero-variance features (fewer than 2 distinct non-NaN values in the
    # training set) crash HistGradientBoostingClassifier's binning step
    # outright, and carry no signal for any model regardless. Confirmed
    # empirically (2026-09-02, see docs/limitations.md): loss_rate/
    # error_rate are exactly 0.0 for every row in this dataset because
    # OVS's rx_dropped/rx_errors port counters never reflect tc-netem
    # -injected loss/corruption on Mininet's veth-based links -- not a
    # bug in this training script. Excluded explicitly and reported, not
    # silently dropped.
    dropped_features = []
    kept_idx = []
    for j, name in enumerate(feature_order):
        col = X_train[:, j]
        n_distinct = len(np.unique(col[~np.isnan(col)]))
        if n_distinct < 2:
            dropped_features.append(name)
        else:
            kept_idx.append(j)
    if dropped_features:
        print(f"\nexcluding {len(dropped_features)} zero-variance features "
              f"(see docs/limitations.md): {dropped_features}\n")
        feature_order = [feature_order[j] for j in kept_idx]
        X_train = X_train[:, kept_idx]
        X_test = X_test[:, kept_idx]

    models = build_models(RANDOM_STATE)
    results = {}
    fitted = {}
    for name, model in models.items():
        t0 = time.time()
        if isinstance(model, Pipeline):
            model.fit(X_train, y_train, clf__sample_weight=sample_weight)
        else:
            model.fit(X_train, y_train, sample_weight=sample_weight)
        fitted[name] = model
        res = evaluate(model, X_test, y_test, threshold=config.ml.threshold)
        res["train_time_s"] = time.time() - t0
        results[name] = res
        print(f"{name}: PR-AUC={res['pr_auc']:.4f} ROC-AUC={res['roc_auc']} "
              f"P={res['precision']:.3f} R={res['recall']:.3f} F1={res['f1']:.3f}")

    best_name = max(results, key=lambda n: results[n]["pr_auc"])
    best_model = fitted[best_name]
    print(f"\nselected best model by PR-AUC: {best_name}")

    perm = permutation_importance(
        best_model, X_test, y_test, scoring="average_precision",
        n_repeats=10, random_state=RANDOM_STATE, n_jobs=-1,
    )
    results[best_name]["permutation_importance"] = {
        "feature_names": feature_order,
        "importances_mean": perm.importances_mean.tolist(),
        "importances_std": perm.importances_std.tolist(),
    }

    models_dir = REPO_ROOT / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(best_model, models_dir / "degradation_model.joblib")

    try:
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        git_commit = None

    metadata = {
        "sklearn_version": sklearn.__version__,
        "chosen_model": best_name,
        "feature_order": feature_order,
        "dropped_zero_variance_features": dropped_features,
        "train_seeds": list(TRAIN_SEEDS),
        "test_seeds": list(TEST_SEEDS),
        "random_state": RANDOM_STATE,
        "threshold": config.ml.threshold,
        "label_horizon_s": config.label.horizon_s,
        "git_commit": git_commit,
        "trained_at": time.time(),
        "data_source": DataSource.EMULATED_MININET.value,
    }
    with open(models_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    figures_dir = REPO_ROOT / "results" / "figures"
    make_figures(results, feature_order, best_name, figures_dir)

    tables_dir = REPO_ROOT / "results" / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "chosen_model": best_name,
        "threshold": config.ml.threshold,
        "threshold_justification": (
            "Fixed at config.ml.threshold, the same value the live "
            "persistence/stability decision logic (Phase 12) uses -- so "
            "reported precision/recall reflect exactly the operating point "
            "the closed loop runs at, not a metric re-optimized after the "
            "fact. This value sits outside persistence.uncertainty_band, "
            "avoiding the abstention band by construction "
            "(see tests/unit/test_config.py::test_ml_threshold_within_bounds)."
        ),
        "train_seeds": list(TRAIN_SEEDS),
        "test_seeds": list(TEST_SEEDS),
        "split_method": "by_run_not_by_row",
        "dropped_zero_variance_features": dropped_features,
        "dropped_features_reason": (
            "loss_rate/error_rate-derived features are exactly 0.0 for every "
            "row: OVS's rx_dropped/rx_errors port counters never reflect "
            "tc-netem-injected loss/corruption on Mininet's veth-based "
            "links, confirmed empirically. See docs/limitations.md."
        ) if dropped_features else None,
        "models": {
            name: {k: v for k, v in res.items() if not k.startswith("_") and k != "permutation_importance"}
            for name, res in results.items()
        },
        "permutation_importance": results[best_name]["permutation_importance"],
    }
    with open(tables_dir / "ml_metrics.json", "w") as f:
        json.dump(report, f, indent=2)

    print(f"\nwrote models/degradation_model.joblib, models/metadata.json, "
          f"results/tables/ml_metrics.json, results/figures/ml_*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
