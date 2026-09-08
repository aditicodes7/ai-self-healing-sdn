#!/usr/bin/env python3
"""Aggregates executed Phase 22 trials into results/tables/
experiment_results.json and prints the comparison tables.

Reads only what the runner actually recorded. Rules it follows, because
each one is a way results like these usually get overstated:

  * A trial that never detected contributes to the DETECTION RATE but
    not to mean detection latency -- averaging latency over only the
    successes and reporting it as "the" latency hides the misses.
  * Trials that errored are excluded from metrics but COUNTED and
    reported, so a partially-failed batch can never look like a clean one.
  * Confidence intervals are reported for every mean, and a mean over
    fewer than 3 samples is marked as such rather than presented as if
    it were solid.
  * Nothing is interpolated. A metric that was never measured stays
    null all the way into the output.

Run (anywhere the results database is readable):
    python experiments/analyze.py
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from database.db import Database  # noqa: E402

MIN_SAMPLES_FOR_CONFIDENCE = 3


def mean_ci95(values: list[float]) -> dict:
    """Mean with a 95% interval. `underpowered` is set rather than
    silently returning a confident-looking number from 1-2 samples."""
    clean = [v for v in values if v is not None]
    n = len(clean)
    if n == 0:
        return {"mean": None, "ci95": None, "n": 0, "underpowered": True}
    mean = statistics.fmean(clean)
    if n == 1:
        return {"mean": mean, "ci95": None, "n": 1, "underpowered": True}
    sd = statistics.stdev(clean)
    return {
        "mean": mean,
        "sd": sd,
        "ci95": 1.96 * sd / math.sqrt(n),
        "n": n,
        "underpowered": n < MIN_SAMPLES_FOR_CONFIDENCE,
    }


def _throughput_ratio(rec: dict) -> float | None:
    """During-fault throughput as a fraction of that trial's OWN
    baseline. Comparing raw Mbps across trials would fold in run-to-run
    baseline variation; each trial is its own control."""
    base = (rec.get("baseline") or {}).get("throughput_bps")
    during = (rec.get("during_fault") or {}).get("throughput_bps")
    if not base or during is None:
        return None
    return during / base


def summarize(records: list[dict]) -> dict:
    usable = [r for r in records if not r.get("error")]
    errored = [r for r in records if r.get("error")]

    detected = [r for r in usable if r.get("detected")]
    recovered = [r for r in usable if r.get("recovery_time_s") is not None]

    return {
        "trials_run": len(records),
        "trials_usable": len(usable),
        "trials_errored": len(errored),
        "errors": [r.get("error") for r in errored],
        # Rate over ALL usable trials -- the misses are in the denominator.
        "detection_rate": (len(detected) / len(usable)) if usable else None,
        "recovery_rate": (len(recovered) / len(usable)) if usable else None,
        "detection_latency_s": mean_ci95([r["detection_latency_s"] for r in detected]),
        "recovery_time_s": mean_ci95([r["recovery_time_s"] for r in recovered]),
        "reroute_count": mean_ci95([r.get("reroute_count") for r in usable]),
        "throughput_retained_frac": mean_ci95([_throughput_ratio(r) for r in usable]),
        "loss_percent_during_fault": mean_ci95(
            [(r.get("during_fault") or {}).get("loss_percent") for r in usable]
        ),
        "rtt_ms_during_fault": mean_ci95(
            [(r.get("during_fault") or {}).get("rtt_avg_ms") for r in usable]
        ),
        "verification_outcomes": {
            outcome: sum(1 for r in usable if r.get("verification") == outcome)
            for outcome in ("SUCCESS", "PARTIAL", "FAILED")
        },
        "rl_fallbacks": sum(len(r.get("rl_fallbacks") or []) for r in usable),
    }


def _fmt(entry: dict, scale: float = 1.0, unit: str = "") -> str:
    if entry is None or entry.get("mean") is None:
        return "n/a"
    mean = entry["mean"] * scale
    if entry.get("ci95") is None:
        return f"{mean:.2f}{unit}*"
    return f"{mean:.2f}±{entry['ci95']*scale:.2f}{unit}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(REPO_ROOT / "results" / "experiments.db"))
    parser.add_argument(
        "--min-id", type=int, default=None,
        help=(
            "Only analyse runs with this row id or higher. The results "
            "database accumulates across sessions, so exploratory trials "
            "(a pilot, a one-off check, a run under a superseded model) "
            "sit alongside a real batch. Mixing them silently changes the "
            "reported rates, so a batch is selected explicitly rather "
            "than by deleting rows -- the earlier trials stay on record."
        ),
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"no results database at {db_path} -- run experiments/runner.py first")
        return 1

    with Database(db_path) as db:
        if args.min_id is not None:
            rows = db.query(
                "SELECT * FROM experiment_runs WHERE id >= ? ORDER BY started_at", (args.min_id,)
            )
        else:
            rows = db.query("SELECT * FROM experiment_runs ORDER BY started_at")

    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        try:
            rec = json.loads(row["results_json"])
        except (ValueError, TypeError):
            continue
        grouped.setdefault((row["experiment_id"], row["system"]), []).append(rec)

    output = {
        "generated_at": time.time(),
        "data_source": "emulated_mininet",
        "total_trials_recorded": len(rows),
        "results": {
            f"{exp}/{system}": summarize(recs) for (exp, system), recs in sorted(grouped.items())
        },
    }

    out_dir = REPO_ROOT / "results" / "tables"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "experiment_results.json").write_text(json.dumps(output, indent=2))

    print(f"{len(rows)} trials recorded\n")
    header = (f"{'experiment / system':<34}{'det%':>7}{'rec%':>7}{'det_s':>13}"
              f"{'recov_s':>13}{'reroutes':>12}{'thr_kept':>12}")
    print(header)
    print("-" * len(header))
    for key, s in output["results"].items():
        det = f"{s['detection_rate']*100:.0f}%" if s["detection_rate"] is not None else "n/a"
        rec = f"{s['recovery_rate']*100:.0f}%" if s["recovery_rate"] is not None else "n/a"
        print(f"{key:<34}{det:>7}{rec:>7}"
              f"{_fmt(s['detection_latency_s']):>13}{_fmt(s['recovery_time_s']):>13}"
              f"{_fmt(s['reroute_count']):>12}{_fmt(s['throughput_retained_frac'], 100, '%'):>12}")

    underpowered = [k for k, s in output["results"].items()
                    if s["trials_usable"] < MIN_SAMPLES_FOR_CONFIDENCE]
    if underpowered:
        print(f"\n* fewer than {MIN_SAMPLES_FOR_CONFIDENCE} usable trials -- not a result yet: "
              + ", ".join(underpowered))
    errored = {k: s["trials_errored"] for k, s in output["results"].items() if s["trials_errored"]}
    if errored:
        print(f"\nerrored trials (excluded from metrics, not from the record): {errored}")
    print(f"\nwrote {out_dir / 'experiment_results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
