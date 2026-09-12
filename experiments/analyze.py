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


# How long the injected fault is actually present, measured from the
# `fault_start` that detection latency is relative to: the injector ramps
# to full intensity over DEFAULT_RAMP_IN_S and then holds for
# FAULT_DURATION_S before recovering. Confirmed against the injector's own
# ground-truth log (results/raw/experiments/*/faults.jsonl): fault_end is
# exactly 150.0-150.1s after full intensity is applied.
FAULT_WINDOW_S = 12.0 + 150.0


def _detected_in_fault(rec: dict) -> bool:
    """True if the detection landed while the fault was still present.

    A "detection" logged after the fault has been removed is not a
    detection of the fault -- it is a reaction to the recovery transient,
    when bandwidth and delay snap back and queues flush. Counting those
    as successes inflates a reactive system's detection rate and, worse,
    inflates its mean detection latency with values that are really just
    "the length of the fault window". In E3 six of B2's ten detections
    landed 4-6s after fault_end, clustered at 166-168s against a 162s
    window -- far too tight to be anything but the removal itself.
    """
    lat = rec.get("detection_latency_s")
    return lat is not None and lat <= FAULT_WINDOW_S


def summarize(records: list[dict]) -> dict:
    usable = [r for r in records if not r.get("error")]
    errored = [r for r in records if r.get("error")]

    detected = [r for r in usable if r.get("detected")]
    in_fault = [r for r in detected if _detected_in_fault(r)]
    post_fault = [r for r in detected if not _detected_in_fault(r)]
    recovered = [r for r in usable if r.get("recovery_time_s") is not None]

    return {
        "trials_run": len(records),
        "trials_usable": len(usable),
        "trials_errored": len(errored),
        "errors": [r.get("error") for r in errored],
        # Rate over ALL usable trials -- the misses are in the denominator.
        "detection_rate": (len(detected) / len(usable)) if usable else None,
        # The rate that should be quoted. `detection_rate` above counts
        # any incident opened during the trial, including ones opened
        # after the fault was already gone.
        "detection_rate_in_fault": (len(in_fault) / len(usable)) if usable else None,
        "detections_after_fault_ended": len(post_fault),
        "detection_latency_in_fault_s": mean_ci95(
            [r["detection_latency_s"] for r in in_fault]
        ),
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
        # E8's evidence. Present only for trials where the inference
        # service was actually killed, so it is None everywhere else
        # rather than a zero that would read as "nothing happened".
        "safety": _safety_summary(usable),
    }


def _safety_summary(usable: list[dict]) -> dict | None:
    """What happened after the inference service was killed.

    The claim is that an unreachable model yields NO prediction rather
    than a guessed one, so the number that matters is predictions after
    the kill, per trial. Reported as a list, not a mean: a single trial
    fabricating one prediction would be a failure, and a mean of 0.2
    would look like rounding error.
    """
    killed = [r for r in usable if r.get("service_killed_at")]
    if not killed:
        return None
    return {
        "trials_with_service_killed": len(killed),
        "predictions_after_kill_per_trial": [
            r.get("n_predictions_after_service_kill") for r in killed
        ],
        "fallbacks_after_kill": sum(
            len(r.get("rl_fallbacks_after_service_kill") or []) for r in killed
        ),
    }


def _variant_of(trial_id: str, experiment_id: str, system: str) -> str:
    """The arm of the experiment this trial belongs to, or "" for the
    default arm.

    E5's ablation and E7's load levels are recorded only as a trial-id
    suffix (`_nogates`, `_bg6`) -- the `system` column is P2_ML_RL for
    every one of them. Grouping by that column alone silently averaged
    E5's two arms into a single row reading 2.40 reroutes, the midpoint
    of a control at 0.6 and an ablation at 4.2, describing neither and
    hiding the entire result. The variant has to be part of the key.
    """
    prefix = f"{experiment_id}_{system}_"
    if not trial_id.startswith(prefix):
        return ""
    _, sep, tail = trial_id[len(prefix):].rpartition("_seed")
    if not sep:
        return ""
    # tail is the seed number plus any variant suffix: "3", "3_nogates".
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    return tail[len(digits):]


def _fault_of(trial_id: str, experiment_id: str, system: str) -> str | None:
    """Recover the fault type from a trial id.

    Ids are built as `{experiment}_{system}_{fault}_seed{n}`, and the
    fault is the only part not already recorded in its own column. Both
    experiment and system are stripped by name rather than by splitting
    on "_", since every one of them contains underscores itself
    (B1_SHORTEST_PATH, gradual_degradation).
    """
    prefix = f"{experiment_id}_{system}_"
    if not trial_id.startswith(prefix):
        return None
    rest = trial_id[len(prefix):]
    fault, _, seed = rest.rpartition("_seed")
    return fault or None


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
    parser.add_argument(
        "--exclude-ids", type=int, nargs="*", default=None,
        help=(
            "Drop specific run ids. --min-id can only cut a prefix, which "
            "is wrong when a single aborted trial sits in the middle of "
            "otherwise good batches -- excluding it by id keeps every "
            "other experiment in the analysis, and the row stays on record."
        ),
    )
    parser.add_argument(
        "--include-invalidated", action="store_true",
        help=(
            "Analyse rows listed in results/invalidated_runs.json too. "
            "Only for inspecting what was discarded -- never for a "
            "reported number."
        ),
    )
    args = parser.parse_args()

    # Runs already ruled invalid are dropped unless explicitly asked for.
    # Relying on whoever runs this to remember the right --exclude-ids
    # failed once already: results/tables/experiment_results.json shipped
    # E2 numbers from a run that had been declared invalid in the very
    # commit that declared it, and the dashboard displayed them as
    # measured evidence. The exclusion list now lives with the data.
    excluded = set(args.exclude_ids or [])
    invalidated_path = REPO_ROOT / "results" / "invalidated_runs.json"
    if not args.include_invalidated and invalidated_path.exists():
        record = json.loads(invalidated_path.read_text())
        for entry in record.get("excluded", []):
            low, high = entry["id_range"]
            excluded.update(range(low, high + 1))
            print(
                f"excluding ids {low}-{high} ({entry['experiment']}): "
                f"{entry['reason'].splitlines()[0]}"
            )
    args.exclude_ids = sorted(excluded)

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
    if args.exclude_ids:
        dropped = set(args.exclude_ids)
        rows = [r for r in rows if r["id"] not in dropped]

    grouped: dict[tuple[str, str], list[dict]] = {}
    by_fault: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        try:
            rec = json.loads(row["results_json"])
        except (ValueError, TypeError):
            continue
        exp_id, system = row["experiment_id"], row["system"]
        trial_id = rec.get("trial_id") or ""
        # Arms of the same experiment (E5's ablation, E7's load levels)
        # share a `system` value and must not be pooled -- see
        # _variant_of.
        arm = system + _variant_of(trial_id, exp_id, system)
        grouped.setdefault((exp_id, arm), []).append(rec)
        fault = _fault_of(trial_id, exp_id, system)
        if fault:
            by_fault.setdefault((exp_id, arm, fault), []).append(rec)

    output = {
        "generated_at": time.time(),
        "data_source": "emulated_mininet",
        "total_trials_recorded": len(rows),
        "results": {
            f"{exp}/{system}": summarize(recs) for (exp, system), recs in sorted(grouped.items())
        },
        # Aggregating a whole experiment into one row is the wrong shape
        # for anything that spans several fault types. E2's entire claim
        # is about WHICH faults are handled and which are not -- a single
        # pooled detection rate averages a fault detected 5/5 together
        # with one detected 0/5 and reports neither.
        "by_fault": {
            f"{exp}/{system}/{fault}": summarize(recs)
            for (exp, system, fault), recs in sorted(by_fault.items())
        },
    }

    out_dir = REPO_ROOT / "results" / "tables"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "experiment_results.json").write_text(json.dumps(output, indent=2))

    print(f"{len(rows)} trials recorded\n")
    header = (f"{'experiment / system':<34}{'det%':>7}{'late':>6}{'rec%':>7}{'det_s':>13}"
              f"{'recov_s':>13}{'reroutes':>12}{'thr_kept':>12}")
    print(header)
    print("-" * len(header))
    for key, s in output["results"].items():
        # det% and det_s are the IN-FAULT figures. A detection logged
        # after the fault was removed is counted under `late` instead of
        # being folded into either.
        rate = s["detection_rate_in_fault"]
        det = f"{rate*100:.0f}%" if rate is not None else "n/a"
        late = str(s["detections_after_fault_ended"] or "")
        rec = f"{s['recovery_rate']*100:.0f}%" if s["recovery_rate"] is not None else "n/a"
        print(f"{key:<34}{det:>7}{late:>6}{rec:>7}"
              f"{_fmt(s['detection_latency_in_fault_s']):>13}{_fmt(s['recovery_time_s']):>13}"
              f"{_fmt(s['reroute_count']):>12}{_fmt(s['throughput_retained_frac'], 100, '%'):>12}")
    print("\n  det% / det_s count only detections made while the fault was still")
    print("  present. 'late' counts incidents opened after the fault had been")
    print("  removed -- reactions to the recovery transient, not detections.")

    # Per-fault detail, printed only where an experiment actually spans
    # more than one fault type -- for a single-fault experiment it would
    # just restate the row above it.
    multi = sorted({
        key.split("/")[0] for key in output["by_fault"]
        if len({k.split("/")[2] for k in output["by_fault"] if k.startswith(key.split("/")[0] + "/")}) > 1
    })
    for exp_id in multi:
        print(f"\n{exp_id} by fault type")
        sub_header = (f"{'system / fault':<44}{'det%':>7}{'n':>4}{'det_s':>13}"
                      f"{'recov_s':>13}{'reroutes':>12}")
        print(sub_header)
        print("-" * len(sub_header))
        for key, s in output["by_fault"].items():
            if not key.startswith(exp_id + "/"):
                continue
            _, system, fault = key.split("/")
            rate = s["detection_rate_in_fault"]
            det = f"{rate*100:.0f}%" if rate is not None else "n/a"
            print(f"{system + ' / ' + fault:<44}{det:>7}{s['trials_usable']:>4}"
                  f"{_fmt(s['detection_latency_in_fault_s']):>13}{_fmt(s['recovery_time_s']):>13}"
                  f"{_fmt(s['reroute_count']):>12}")
    print()

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
