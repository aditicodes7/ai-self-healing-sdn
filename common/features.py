"""Feature pipeline shared, byte-for-byte, between ML training (Phase 9,
batch over a whole recorded run) and live controller inference (Phase 10,
one new sample at a time). This is the single place either path is allowed
to compute a feature vector -- see tests/unit/test_features.py's
train/live equality test, which feeds the same raw samples through both a
batch call pattern and an incremental call pattern and asserts identical
output at every step.

Pure Python + numpy only (no pandas): this module is imported unchanged by
both venv-ctrl (Python 3.9, no pandas installed) and venv-ml (Python 3.11).

NaN/missing-data policy (explicit, never silent fillna(0)): any statistic
that needs more history than currently exists (e.g. a 10-sample rolling
mean when only 4 samples have arrived) is `None` in the output dict, and
the caller (ML training, live inference) must treat `None` as "not yet
computable", never as zero -- a `None` loss rate is not the same claim as
a measured loss rate of 0.0.
"""
from __future__ import annotations

import dataclasses

import numpy as np

# Base per-window metrics whose rolling statistics we compute. "loss" is
# named to match docs/architecture.md's derived-feature list; the RawWindow
# field is loss_rate for clarity at the point of computation.
_METRICS = ("utilization", "loss_rate", "latency_ms", "error_rate")


@dataclasses.dataclass(frozen=True)
class RawWindow:
    """One link's derived base metrics at one point in time -- the output
    of compute_base_metrics(), and the sole input to compute_feature_vector().
    """

    timestamp: float
    link_id: str
    utilization: float | None
    loss_rate: float | None
    error_rate: float | None
    latency_ms: float | None
    packet_rate: float | None


def compute_base_metrics(
    prev_counters: dict, curr_counters: dict, dt_s: float, capacity_bps: float | None
) -> RawWindow | None:
    """Derive one RawWindow from two consecutive raw `telemetry` rows for
    the same (switch, port). Formulas match docs/architecture.md:
        throughput   = delta_bytes / delta_t
        utilization  = throughput / configured_link_capacity
        loss_rate    = delta_dropped / (delta_packets + delta_dropped)
        error_rate   = delta_errors / delta_packets
        packet_rate  = delta_packets / delta_t

    Returns None (not a RawWindow with zeroed fields) if dt_s <= 0 or any
    counter delta is negative -- a counter reset or clock issue means these
    two samples cannot honestly be compared, so no metric is computed
    rather than emitting a misleading value derived from garbage deltas.
    """
    if dt_s <= 0:
        return None

    delta_bytes = curr_counters["rx_bytes"] - prev_counters["rx_bytes"]
    delta_rx_packets = curr_counters["rx_packets"] - prev_counters["rx_packets"]
    delta_tx_packets = curr_counters["tx_packets"] - prev_counters["tx_packets"]
    delta_packets = delta_rx_packets + delta_tx_packets
    delta_dropped = (
        (curr_counters["rx_dropped"] - prev_counters["rx_dropped"])
        + (curr_counters["tx_dropped"] - prev_counters["tx_dropped"])
    )
    delta_errors = (
        (curr_counters["rx_errors"] - prev_counters["rx_errors"])
        + (curr_counters["tx_errors"] - prev_counters["tx_errors"])
    )

    if delta_bytes < 0 or delta_packets < 0 or delta_dropped < 0 or delta_errors < 0:
        return None

    throughput_bps = (delta_bytes * 8) / dt_s
    utilization = (throughput_bps / capacity_bps) if capacity_bps else None

    denom_loss = delta_packets + delta_dropped
    loss_rate = (delta_dropped / denom_loss) if denom_loss > 0 else 0.0

    error_rate = (delta_errors / delta_packets) if delta_packets > 0 else 0.0

    packet_rate = delta_packets / dt_s

    return RawWindow(
        timestamp=curr_counters["timestamp"],
        link_id=curr_counters["link_id"],
        utilization=utilization,
        loss_rate=loss_rate,
        error_rate=error_rate,
        latency_ms=curr_counters.get("latency_ms"),
        packet_rate=packet_rate,
    )


def _rolling_mean_std(values: list[float | None], window: int) -> tuple[float | None, float | None]:
    if len(values) < window:
        return None, None
    recent = values[-window:]
    if any(v is None for v in recent):
        return None, None
    arr = np.asarray(recent, dtype=float)
    return float(arr.mean()), float(arr.std(ddof=0))


def _linear_fit_slope(values: list[float | None], window: int) -> float | None:
    if len(values) < window:
        return None
    recent = values[-window:]
    if any(v is None for v in recent):
        return None
    x = np.arange(window, dtype=float)
    y = np.asarray(recent, dtype=float)
    slope, _intercept = np.polyfit(x, y, 1)
    return float(slope)


def _first_difference(values: list[float | None]) -> float | None:
    if len(values) < 2:
        return None
    prev, curr = values[-2], values[-1]
    if prev is None or curr is None:
        return None
    return curr - prev


def _ewma_baseline(values: list[float | None], baseline_windows: int, alpha: float) -> float | None:
    """EWMA over the FIRST `baseline_windows` samples, fixed once
    established -- not a continuously-updated smoother. `values` here is
    the metric's full history up to and including the current sample; the
    baseline only uses values[:baseline_windows], per docs/architecture.md
    ("built from the first N healthy windows")."""
    if len(values) < baseline_windows:
        return None
    healthy = values[:baseline_windows]
    if any(v is None for v in healthy):
        return None
    baseline = healthy[0]
    for v in healthy[1:]:
        baseline = alpha * v + (1 - alpha) * baseline
    return float(baseline)


# Below this, a baseline is treated as too close to zero to divide by.
# A ratio against a ~0 baseline is not "very large", it is undefined --
# and the explicit None keeps that distinct from a genuine 0.0, matching
# this module's "never silently substitute a number" policy.
_MIN_BASELINE_FOR_RATIO = 1e-9


def _safe_ratio(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None or abs(baseline) < _MIN_BASELINE_FOR_RATIO:
        return None
    return float(value / baseline)


def compute_feature_vector(
    history: list[RawWindow],
    index: int,
    windows: tuple[int, ...] = (3, 5, 10),
    ewma_alpha: float = 0.2,
    baseline_windows: int = 20,
) -> dict:
    """The feature vector for history[index], using ONLY history[:index+1]
    (never later samples -- this is what makes the same function safe to
    call both in a batch loop over a whole run and incrementally as each
    live sample arrives, and is what tests/unit/test_features.py's
    equality test verifies).

    Returns a flat dict: for each of utilization/loss_rate/latency_ms/
    error_rate -- mean_{w}/std_{w}/slope_{w} for each w in `windows`,
    diff_1 (first difference from the immediately preceding sample), and
    ewma_dev (deviation from the per-link EWMA baseline). Plus the current
    sample's own packet_rate (not windowed -- a single instantaneous
    value). Any entry that cannot yet be computed (insufficient history,
    a None in the required range) is explicitly None, never 0.
    """
    if index < 0 or index >= len(history):
        raise IndexError(f"index {index} out of range for history of length {len(history)}")

    truncated = history[: index + 1]
    features: dict[str, float | None] = {}

    for metric in _METRICS:
        series = [getattr(w, metric) for w in truncated]

        for w in windows:
            mean, std = _rolling_mean_std(series, w)
            features[f"{metric}_mean_{w}"] = mean
            features[f"{metric}_std_{w}"] = std
            features[f"{metric}_slope_{w}"] = _linear_fit_slope(series, w)

        features[f"{metric}_diff_1"] = _first_difference(series)

        baseline = _ewma_baseline(series, baseline_windows, ewma_alpha)
        current = series[-1]
        features[f"{metric}_ewma_dev"] = (
            (current - baseline) if (baseline is not None and current is not None) else None
        )

        # SCALE-INVARIANT counterparts. Everything above is an absolute
        # magnitude, so the same proportional degradation produces a
        # completely different feature vector depending on the ambient
        # level -- a link going 4ms -> 8ms and one going 40ms -> 80ms are
        # both a doubling, but ewma_dev reports 4 vs 40.
        #
        # That is not hypothetical: Phase 22 measured the controller's
        # own probe latency at a 2.6ms median during dataset generation
        # and 40.9ms under the experiment harness's load, and the model
        # -- whose most important features are latency-derived -- scored
        # a healthy link (0.720) above one whose latency had gone
        # 40.9 -> 342ms (0.290). Ratios transfer across operating points
        # where absolute magnitudes do not.
        #
        # Kept ALONGSIDE the absolute features rather than replacing
        # them: absolute level still carries real information (a link at
        # 300ms is bad regardless of its history), and dropping it would
        # trade one blind spot for another.
        features[f"{metric}_ewma_ratio"] = _safe_ratio(current, baseline)
        for w in windows:
            features[f"{metric}_mean_ratio_{w}"] = _safe_ratio(
                features[f"{metric}_mean_{w}"], baseline
            )

    features["packet_rate"] = truncated[-1].packet_rate
    return features
