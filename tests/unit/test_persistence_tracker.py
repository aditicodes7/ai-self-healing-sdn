import pytest

from controller.persistence_tracker import PersistenceTracker

WINDOW = 4
CONSECUTIVE = 3
BAND = (0.45, 0.60)
THRESHOLD = 0.75


def _tracker() -> PersistenceTracker:
    return PersistenceTracker(window=WINDOW, consecutive=CONSECUTIVE, uncertainty_band=BAND, threshold=THRESHOLD)


def test_no_readings_yet_is_not_persistent():
    tracker = _tracker()
    result = tracker.evaluate("s1-s2")
    assert result.persistent_degradation is False
    assert result.uncertain is False
    assert "no readings" in result.reason


def test_single_high_reading_not_yet_persistent():
    tracker = _tracker()
    tracker.record("s1-s2", 0.90)
    result = tracker.evaluate("s1-s2")
    assert result.persistent_degradation is False
    assert result.consecutive_count == 1


def test_persistence_confirmed_at_three_of_four():
    tracker = _tracker()
    for p in (0.80, 0.40, 0.85, 0.90):  # 3 of 4 >= threshold, latest is high
        tracker.record("s1-s2", p)
    result = tracker.evaluate("s1-s2")
    assert result.persistent_degradation is True
    assert result.consecutive_count == 3
    assert "persistence confirmed" in result.reason


def test_latest_below_threshold_is_never_persistent_even_if_history_qualifies():
    tracker = _tracker()
    for p in (0.90, 0.90, 0.90, 0.30):  # 3 of 4 above threshold, but latest is low
        tracker.record("s1-s2", p)
    result = tracker.evaluate("s1-s2")
    assert result.persistent_degradation is False


def test_only_two_of_four_above_threshold_not_persistent():
    tracker = _tracker()
    for p in (0.80, 0.40, 0.40, 0.90):  # only 2 of 4 >= threshold
        tracker.record("s1-s2", p)
    result = tracker.evaluate("s1-s2")
    assert result.persistent_degradation is False
    assert result.consecutive_count == 2


def test_uncertainty_band_abstention_overrides_everything():
    tracker = _tracker()
    for p in (0.90, 0.90, 0.90, 0.50):  # history all high, but latest lands in the band
        tracker.record("s1-s2", p)
    result = tracker.evaluate("s1-s2")
    assert result.uncertain is True
    assert result.persistent_degradation is False
    assert "uncertain" in result.reason


def test_window_evicts_old_readings():
    tracker = _tracker()
    for p in (0.90, 0.90, 0.90, 0.90, 0.10, 0.10, 0.10):  # last 4 are 0.90,0.10,0.10,0.10
        tracker.record("s1-s2", p)
    result = tracker.evaluate("s1-s2")
    assert result.window_size == WINDOW
    assert result.persistent_degradation is False  # latest is 0.10, below threshold


def test_links_tracked_independently():
    tracker = _tracker()
    for p in (0.90, 0.90, 0.90, 0.90):
        tracker.record("s1-s2", p)
    tracker.record("s3-s6", 0.10)

    assert tracker.evaluate("s1-s2").persistent_degradation is True
    assert tracker.evaluate("s3-s6").persistent_degradation is False


def test_consecutive_greater_than_window_is_rejected_at_construction():
    with pytest.raises(ValueError):
        PersistenceTracker(window=3, consecutive=4, uncertainty_band=BAND, threshold=THRESHOLD)
