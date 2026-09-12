"""Tests for experiments/analyze.py.

These are mostly tests that the aggregator does not OVERSTATE results --
each one corresponds to a specific way a comparison table can be made to
look better than the underlying trials justify.
"""
from __future__ import annotations

import pytest

from experiments.analyze import mean_ci95, summarize


def _trial(**over):
    base = {
        "detected": True,
        "detection_latency_s": 10.0,
        "recovery_time_s": 20.0,
        "reroute_count": 1,
        "baseline": {"throughput_bps": 4_000_000},
        "during_fault": {"throughput_bps": 2_000_000, "loss_percent": 0.0, "rtt_avg_ms": 50.0},
        "verification": "SUCCESS",
        "rl_fallbacks": [],
    }
    base.update(over)
    return base


# -- mean_ci95 -----------------------------------------------------------


def test_mean_ci95_basic():
    r = mean_ci95([1.0, 2.0, 3.0, 4.0, 5.0])
    assert r["mean"] == pytest.approx(3.0)
    assert r["n"] == 5
    assert r["underpowered"] is False


def test_single_sample_has_no_confidence_interval():
    r = mean_ci95([4.2])
    assert r["mean"] == 4.2
    assert r["ci95"] is None
    assert r["underpowered"] is True


def test_empty_returns_none_not_zero():
    """A metric that was never measured must not become 0.0."""
    r = mean_ci95([])
    assert r["mean"] is None
    assert r["n"] == 0


def test_nones_are_excluded_not_counted_as_zero():
    r = mean_ci95([2.0, None, 2.0])
    assert r["mean"] == pytest.approx(2.0)
    assert r["n"] == 2


# -- summarize -----------------------------------------------------------


def test_missed_detections_stay_in_the_denominator():
    """The failure mode this guards: reporting a detection rate computed
    only over trials that detected, which is always 100%."""
    s = summarize([_trial(), _trial(detected=False, detection_latency_s=None,
                                    recovery_time_s=None, verification=None)])
    assert s["detection_rate"] == 0.5


def test_detection_latency_averages_only_over_detections():
    """...but the LATENCY average must exclude non-detections rather than
    treat them as zero-latency, which would flatter the result."""
    s = summarize([
        _trial(detection_latency_s=10.0),
        _trial(detected=False, detection_latency_s=None, recovery_time_s=None),
    ])
    assert s["detection_latency_s"]["n"] == 1
    assert s["detection_latency_s"]["mean"] == pytest.approx(10.0)


def test_errored_trials_are_excluded_from_metrics_but_counted():
    s = summarize([_trial(), {"error": "network never came up"}])
    assert s["trials_run"] == 2
    assert s["trials_usable"] == 1
    assert s["trials_errored"] == 1
    assert s["errors"] == ["network never came up"]
    assert s["detection_rate"] == 1.0  # over the one usable trial


def test_a_batch_that_all_errored_reports_no_metrics():
    s = summarize([{"error": "boom"}, {"error": "boom"}])
    assert s["trials_usable"] == 0
    assert s["detection_rate"] is None
    assert s["recovery_time_s"]["mean"] is None


def test_throughput_is_relative_to_each_trials_own_baseline():
    """Raw Mbps across trials would fold in baseline variation; each
    trial is its own control."""
    s = summarize([
        _trial(baseline={"throughput_bps": 4_000_000},
               during_fault={"throughput_bps": 2_000_000, "loss_percent": 0.0, "rtt_avg_ms": 1.0}),
        _trial(baseline={"throughput_bps": 8_000_000},
               during_fault={"throughput_bps": 4_000_000, "loss_percent": 0.0, "rtt_avg_ms": 1.0}),
    ])
    # Both retained exactly half, despite different absolute baselines.
    assert s["throughput_retained_frac"]["mean"] == pytest.approx(0.5)


def test_missing_baseline_yields_no_ratio_rather_than_a_guess():
    s = summarize([_trial(baseline={"throughput_bps": None})])
    assert s["throughput_retained_frac"]["n"] == 0


def test_small_sample_is_flagged_underpowered():
    s = summarize([_trial(), _trial()])
    assert s["recovery_time_s"]["underpowered"] is True


def test_rl_fallbacks_are_counted():
    s = summarize([_trial(rl_fallbacks=["rl_service_unavailable"]), _trial()])
    assert s["rl_fallbacks"] == 1


def test_verification_outcomes_are_tallied():
    s = summarize([_trial(verification="SUCCESS"), _trial(verification="FAILED")])
    assert s["verification_outcomes"]["SUCCESS"] == 1
    assert s["verification_outcomes"]["FAILED"] == 1


def test_experiment_arms_are_not_pooled_into_one_row():
    """E5's ablation and E7's load levels share a `system` value.

    They are distinguished only by a trial-id suffix, so grouping on the
    database's system column alone averaged E5's control (0.6 reroutes)
    with its ablation (4.2) into a single row reading 2.40 -- a number
    describing neither arm, which hid the entire result.
    """
    from experiments.analyze import _variant_of

    assert _variant_of("E5_P2_ML_RL_gradual_degradation_seed3", "E5", "P2_ML_RL") == ""
    assert _variant_of(
        "E5_P2_ML_RL_gradual_degradation_seed3_nogates", "E5", "P2_ML_RL"
    ) == "_nogates"
    assert _variant_of("E7_P2_ML_RL_congestion_seed10_bg6", "E7", "P2_ML_RL") == "_bg6"
    # A two-digit seed must not eat into the suffix.
    assert _variant_of("E7_P2_ML_RL_congestion_seed12", "E7", "P2_ML_RL") == ""


def test_fault_type_survives_a_variant_suffix():
    """The fault must still parse when an arm suffix follows the seed,
    or per-fault breakdowns silently lose every ablation trial."""
    from experiments.analyze import _fault_of

    assert _fault_of(
        "E5_P2_ML_RL_gradual_degradation_seed3_nogates", "E5", "P2_ML_RL"
    ) == "gradual_degradation"
