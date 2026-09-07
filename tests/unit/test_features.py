import math

import pytest

from common.features import RawWindow, compute_base_metrics, compute_feature_vector


def _counters(t, rx_bytes, tx_bytes=0, rx_packets=0, tx_packets=0, rx_dropped=0,
              tx_dropped=0, rx_errors=0, tx_errors=0, link_id="s1-s2", latency_ms=None):
    return {
        "timestamp": t, "link_id": link_id, "rx_bytes": rx_bytes, "tx_bytes": tx_bytes,
        "rx_packets": rx_packets, "tx_packets": tx_packets, "rx_dropped": rx_dropped,
        "tx_dropped": tx_dropped, "rx_errors": rx_errors, "tx_errors": tx_errors,
        "latency_ms": latency_ms,
    }


def _mk_window(i, link_id="s1-s2", utilization=None, loss_rate=None, error_rate=None,
               latency_ms=None, packet_rate=None):
    return RawWindow(
        timestamp=float(i), link_id=link_id,
        utilization=utilization, loss_rate=loss_rate, error_rate=error_rate,
        latency_ms=latency_ms, packet_rate=packet_rate,
    )


# -- compute_base_metrics --------------------------------------------------

def test_compute_base_metrics_normal_case():
    prev = _counters(0, rx_bytes=1000, rx_packets=10, tx_packets=10)
    curr = _counters(2, rx_bytes=3000, rx_packets=30, tx_packets=30)
    row = compute_base_metrics(prev, curr, dt_s=2.0, capacity_bps=10_000_000)
    assert row is not None
    # delta_bytes=2000, throughput_bps = 2000*8/2 = 8000
    assert row.utilization == pytest.approx(8000 / 10_000_000)
    assert row.loss_rate == 0.0
    assert row.error_rate == 0.0
    assert row.packet_rate == pytest.approx(40 / 2.0)


def test_compute_base_metrics_counter_reset_returns_none():
    prev = _counters(0, rx_bytes=5000, rx_packets=50)
    curr = _counters(2, rx_bytes=1000, rx_packets=10)  # counters went backwards
    assert compute_base_metrics(prev, curr, dt_s=2.0, capacity_bps=1_000_000) is None


def test_compute_base_metrics_nonpositive_dt_returns_none():
    prev = _counters(0, rx_bytes=1000)
    curr = _counters(0, rx_bytes=2000)
    assert compute_base_metrics(prev, curr, dt_s=0.0, capacity_bps=1_000_000) is None
    assert compute_base_metrics(prev, curr, dt_s=-1.0, capacity_bps=1_000_000) is None


def test_compute_base_metrics_no_capacity_gives_none_utilization_not_zero():
    prev = _counters(0, rx_bytes=1000, rx_packets=10, tx_packets=10)
    curr = _counters(2, rx_bytes=3000, rx_packets=30, tx_packets=30)
    row = compute_base_metrics(prev, curr, dt_s=2.0, capacity_bps=None)
    assert row.utilization is None  # not 0.0 -- capacity truly unknown


def test_compute_base_metrics_loss_and_error_rates():
    prev = _counters(0, rx_bytes=0, rx_packets=0, tx_packets=0, rx_dropped=0, rx_errors=0)
    curr = _counters(1, rx_bytes=1000, rx_packets=90, tx_packets=0, rx_dropped=10, rx_errors=5)
    row = compute_base_metrics(prev, curr, dt_s=1.0, capacity_bps=1_000_000)
    # delta_packets = 90, delta_dropped = 10 -> loss_rate = 10/(90+10) = 0.1
    assert row.loss_rate == pytest.approx(0.1)
    # error_rate = delta_errors / delta_packets = 5/90
    assert row.error_rate == pytest.approx(5 / 90)


# -- rolling stats / NaN policy --------------------------------------------

def test_insufficient_history_gives_none_not_zero():
    history = [_mk_window(i, utilization=0.5, loss_rate=0.0, error_rate=0.0, latency_ms=1.0,
                           packet_rate=10.0) for i in range(2)]
    features = compute_feature_vector(history, index=1, windows=(3, 5, 10))
    assert features["utilization_mean_3"] is None
    assert features["utilization_std_3"] is None
    assert features["utilization_slope_3"] is None
    assert features["utilization_mean_10"] is None


def test_rolling_mean_std_matches_manual_computation():
    values = [0.1, 0.2, 0.3, 0.4, 0.5]
    history = [
        _mk_window(i, utilization=v, loss_rate=0.0, error_rate=0.0, latency_ms=1.0, packet_rate=1.0)
        for i, v in enumerate(values)
    ]
    features = compute_feature_vector(history, index=4, windows=(3, 5, 10))
    # last 3 values: 0.3, 0.4, 0.5
    expected_mean_3 = (0.3 + 0.4 + 0.5) / 3
    assert features["utilization_mean_3"] == pytest.approx(expected_mean_3)
    # population std of [0.3, 0.4, 0.5]
    expected_std_3 = math.sqrt(sum((x - expected_mean_3) ** 2 for x in (0.3, 0.4, 0.5)) / 3)
    assert features["utilization_std_3"] == pytest.approx(expected_std_3)
    # only 5 samples exist -> window 10 not computable
    assert features["utilization_mean_10"] is None


def test_first_difference():
    history = [
        _mk_window(0, utilization=0.2, loss_rate=0.0, error_rate=0.0, latency_ms=1.0, packet_rate=1.0),
        _mk_window(1, utilization=0.5, loss_rate=0.0, error_rate=0.0, latency_ms=1.0, packet_rate=1.0),
    ]
    features = compute_feature_vector(history, index=1)
    assert features["utilization_diff_1"] == pytest.approx(0.3)


def test_linear_fit_slope_on_perfectly_linear_series():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    history = [
        _mk_window(i, utilization=v, loss_rate=0.0, error_rate=0.0, latency_ms=1.0, packet_rate=1.0)
        for i, v in enumerate(values)
    ]
    features = compute_feature_vector(history, index=4, windows=(5,))
    assert features["utilization_slope_5"] == pytest.approx(1.0)


def test_ewma_baseline_deviation_established_after_n_windows():
    # First 3 "healthy" windows all exactly 0.1 -> baseline settles at 0.1.
    values = [0.1, 0.1, 0.1, 0.9]
    history = [
        _mk_window(i, utilization=v, loss_rate=0.0, error_rate=0.0, latency_ms=1.0, packet_rate=1.0)
        for i, v in enumerate(values)
    ]
    features = compute_feature_vector(history, index=3, windows=(3,), baseline_windows=3)
    assert features["utilization_ewma_dev"] == pytest.approx(0.9 - 0.1)


def test_ewma_baseline_none_before_established():
    history = [
        _mk_window(i, utilization=0.5, loss_rate=0.0, error_rate=0.0, latency_ms=1.0, packet_rate=1.0)
        for i in range(2)
    ]
    features = compute_feature_vector(history, index=1, baseline_windows=20)
    assert features["utilization_ewma_dev"] is None


def test_packet_rate_is_passthrough_of_current_sample():
    history = [_mk_window(0, utilization=0.1, packet_rate=42.0)]
    features = compute_feature_vector(history, index=0)
    assert features["packet_rate"] == 42.0


# -- the train/live equality test (Phase 6's acceptance gate) -------------

def test_train_and_live_paths_produce_identical_feature_vectors():
    """Feeds the same raw samples through a batch ("training") call
    pattern and an incremental ("live") call pattern and asserts every
    single feature vector is identical. This is the actual Phase 6 gate:
    a shared, deterministic function used the same way by both paths, with
    no accidental use of future samples or divergent state."""
    rng_values = [0.10, 0.12, 0.30, 0.08, 0.45, 0.22, 0.19, 0.51, 0.33, 0.07,
                  0.60, 0.15, 0.28, 0.44, 0.09, 0.21, 0.37, 0.13, 0.26, 0.48,
                  0.31, 0.17, 0.55, 0.03, 0.40]
    full_history = [
        _mk_window(
            i, utilization=v, loss_rate=v / 10, error_rate=v / 100,
            latency_ms=v * 10, packet_rate=v * 100,
        )
        for i, v in enumerate(rng_values)
    ]

    # "Training" path: one batch call per index over the whole recorded run.
    batch_results = [
        compute_feature_vector(full_history, i) for i in range(len(full_history))
    ]

    # "Live" path: history grows one sample at a time, as it would from a
    # real controller receiving telemetry incrementally.
    live_results = []
    live_history: list[RawWindow] = []
    for window in full_history:
        live_history.append(window)
        live_results.append(compute_feature_vector(live_history, len(live_history) - 1))

    assert len(batch_results) == len(live_results) == len(full_history)
    for i, (batch_fv, live_fv) in enumerate(zip(batch_results, live_results)):
        assert batch_fv == live_fv, f"mismatch at index {i}: {batch_fv} != {live_fv}"


def _steady_then_double(ambient_scale: float) -> dict:
    """20 steady windows at `4 * scale` ms, then one at double that."""
    history = [
        RawWindow(timestamp=float(i), link_id="s1-s2", utilization=0.3, loss_rate=0.0,
                  error_rate=0.0, latency_ms=4.0 * ambient_scale, packet_rate=100.0)
        for i in range(20)
    ]
    history.append(
        RawWindow(timestamp=20.0, link_id="s1-s2", utilization=0.3, loss_rate=0.0,
                  error_rate=0.0, latency_ms=8.0 * ambient_scale, packet_rate=100.0)
    )
    return compute_feature_vector(history, len(history) - 1)


def test_ratio_features_are_scale_invariant():
    """The same proportional degradation must produce the same ratio
    feature regardless of the ambient level. Phase 22 measured the
    controller's own probe latency at a 2.6ms median during dataset
    generation and 40.9ms under experiment load; absolute features could
    not transfer across that gap."""
    low = _steady_then_double(1.0)
    high = _steady_then_double(10.0)

    assert low["latency_ms_ewma_ratio"] == pytest.approx(2.0)
    assert high["latency_ms_ewma_ratio"] == pytest.approx(2.0)
    assert low["latency_ms_ewma_ratio"] == pytest.approx(high["latency_ms_ewma_ratio"])


def test_absolute_features_are_not_scale_invariant():
    """The complement of the test above: this is WHY the ratio features
    were added, and if absolute features ever became scale-invariant the
    reasoning above would need revisiting."""
    low = _steady_then_double(1.0)
    high = _steady_then_double(10.0)
    assert low["latency_ms_ewma_dev"] == pytest.approx(4.0)
    assert high["latency_ms_ewma_dev"] == pytest.approx(40.0)


def test_ratio_is_none_when_baseline_is_zero():
    """A ratio against a ~0 baseline is undefined, not enormous -- and
    None must stay distinguishable from a real 0.0."""
    history = [
        RawWindow(timestamp=float(i), link_id="s1-s2", utilization=0.0, loss_rate=0.0,
                  error_rate=0.0, latency_ms=0.0, packet_rate=0.0)
        for i in range(21)
    ]
    features = compute_feature_vector(history, len(history) - 1)
    assert features["latency_ms_ewma_ratio"] is None


def test_ratio_is_none_before_baseline_is_established():
    history = [
        RawWindow(timestamp=float(i), link_id="s1-s2", utilization=0.3, loss_rate=0.0,
                  error_rate=0.0, latency_ms=4.0, packet_rate=100.0)
        for i in range(5)  # fewer than baseline_healthy_windows
    ]
    features = compute_feature_vector(history, len(history) - 1)
    assert features["latency_ms_ewma_ratio"] is None
    assert features["latency_ms_mean_ratio_3"] is None
