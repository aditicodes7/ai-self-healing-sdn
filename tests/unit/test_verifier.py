import time

import pytest

from controller.verifier import Baseline, Verifier, classify
from common.schema import IncidentStatus, VerificationClassification
from database.db import Database

LATENCY_TOL = 0.20
LOSS_TOL = 0.20
SETTLE_S = 8.0
WINDOWS = 3
MAX_RETRIES = 2


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "test.db") as database:
        yield database


def _open_incident(db, incident_id: str, link_id: str = "s1-s3") -> None:
    db.insert(
        "incidents",
        {"incident_id": incident_id, "link_id": link_id, "opened_at": time.time(), "status": "OPEN"},
    )


@pytest.fixture
def verifier(db):
    return Verifier(db, settle_s=SETTLE_S, windows=WINDOWS, latency_tolerance=LATENCY_TOL, loss_tolerance=LOSS_TOL, max_retries=MAX_RETRIES)


# -- classify() ---------------------------------------------------------

def test_classify_success_within_tolerance():
    baseline = Baseline(latency_ms=5.0, loss_rate=0.01)
    outcome = classify(baseline, post_latency_ms=5.5, post_loss_rate=0.011, latency_tolerance=LATENCY_TOL, loss_tolerance=LOSS_TOL)
    assert outcome.classification == VerificationClassification.SUCCESS


def test_classify_partial_when_only_one_metric_ok():
    baseline = Baseline(latency_ms=5.0, loss_rate=0.01)
    outcome = classify(baseline, post_latency_ms=5.1, post_loss_rate=0.5, latency_tolerance=LATENCY_TOL, loss_tolerance=LOSS_TOL)
    assert outcome.classification == VerificationClassification.PARTIAL


def test_classify_failed_when_both_metrics_bad():
    baseline = Baseline(latency_ms=5.0, loss_rate=0.01)
    outcome = classify(baseline, post_latency_ms=50.0, post_loss_rate=0.5, latency_tolerance=LATENCY_TOL, loss_tolerance=LOSS_TOL)
    assert outcome.classification == VerificationClassification.FAILED


def test_classify_failed_when_no_telemetry_available():
    baseline = Baseline(latency_ms=5.0, loss_rate=0.01)
    outcome = classify(baseline, post_latency_ms=None, post_loss_rate=0.01, latency_tolerance=LATENCY_TOL, loss_tolerance=LOSS_TOL)
    assert outcome.classification == VerificationClassification.FAILED
    assert "no post-remediation telemetry" in outcome.reason


def test_classify_zero_baseline_loss_uses_absolute_tolerance():
    # Healthy baseline had literally 0 loss -- relative comparison (0 *
    # 1.2 = 0) would impossibly demand exactly 0 post-loss too. Falls
    # back to treating the tolerance fraction as an absolute ceiling.
    baseline = Baseline(latency_ms=5.0, loss_rate=0.0)
    ok = classify(baseline, post_latency_ms=5.0, post_loss_rate=0.15, latency_tolerance=LATENCY_TOL, loss_tolerance=LOSS_TOL)
    assert ok.classification == VerificationClassification.SUCCESS
    bad = classify(baseline, post_latency_ms=5.0, post_loss_rate=0.9, latency_tolerance=LATENCY_TOL, loss_tolerance=LOSS_TOL)
    assert bad.classification != VerificationClassification.SUCCESS


# -- Verifier.verify_and_retry() -----------------------------------------

def test_immediate_success_closes_incident_as_success(db, verifier):
    _open_incident(db, "inc-1")
    baseline = Baseline(latency_ms=5.0, loss_rate=0.01)
    call_count = {"remediate": 0}

    def remediate_fn(excluded):
        call_count["remediate"] += 1
        return [1, 4, 5, 6]

    def measure_fn():
        return 5.2, 0.01  # matches baseline closely

    outcome = verifier.verify_and_retry(
        "inc-1", baseline, remediate_fn, measure_fn, sleep_fn=lambda s: None,
    )

    assert outcome.classification == VerificationClassification.SUCCESS
    assert call_count["remediate"] == 1

    incident = db.query_one("SELECT * FROM incidents WHERE incident_id = ?", ("inc-1",))
    assert incident["status"] == "SUCCESS"
    assert incident["closed_at"] is not None

    rows = db.query("SELECT * FROM verification_results WHERE incident_id = ?", ("inc-1",))
    assert len(rows) == 1
    assert rows[0]["retry_count"] == 0


def test_failed_then_success_on_retry(db, verifier):
    _open_incident(db, "inc-1")
    baseline = Baseline(latency_ms=5.0, loss_rate=0.01)
    attempts = {"n": 0}

    def remediate_fn(excluded):
        attempts["n"] += 1
        return [1, 3, 6] if attempts["n"] == 1 else [1, 4, 5, 6]

    def measure_fn():
        # First attempt looks bad, second looks good.
        return (50.0, 0.5) if attempts["n"] == 1 else (5.1, 0.01)

    outcome = verifier.verify_and_retry(
        "inc-1", baseline, remediate_fn, measure_fn, sleep_fn=lambda s: None,
    )

    assert outcome.classification == VerificationClassification.SUCCESS
    assert attempts["n"] == 2

    incident = db.query_one("SELECT * FROM incidents WHERE incident_id = ?", ("inc-1",))
    assert incident["status"] == "SUCCESS"

    rows = db.query("SELECT * FROM verification_results WHERE incident_id = ? ORDER BY retry_count", ("inc-1",))
    assert len(rows) == 2
    assert rows[0]["classification"] == "FAILED"
    assert rows[1]["classification"] == "SUCCESS"


def test_second_attempt_excludes_the_first_failed_path(db, verifier):
    _open_incident(db, "inc-1")
    baseline = Baseline(latency_ms=5.0, loss_rate=0.01)
    seen_exclusions = []

    def remediate_fn(excluded):
        seen_exclusions.append(list(excluded))
        return [1, 3, 6] if not excluded else [1, 4, 5, 6]

    def measure_fn():
        return (50.0, 0.5) if len(seen_exclusions) == 1 else (5.1, 0.01)

    verifier.verify_and_retry("inc-1", baseline, remediate_fn, measure_fn, sleep_fn=lambda s: None)

    assert seen_exclusions[0] == []
    assert seen_exclusions[1] == [[1, 3, 6]]


def test_sleep_called_with_settle_s_each_attempt(db, verifier):
    _open_incident(db, "inc-1")
    baseline = Baseline(latency_ms=5.0, loss_rate=0.01)
    sleeps = []

    verifier.verify_and_retry(
        "inc-1", baseline,
        remediate_fn=lambda excluded: [1, 4, 5, 6],
        measure_fn=lambda: (5.0, 0.01),
        sleep_fn=lambda s: sleeps.append(s),
    )

    assert sleeps == [SETTLE_S]


def test_no_alternate_path_available_is_unresolved_after_one_attempt(db, verifier):
    _open_incident(db, "inc-1")
    baseline = Baseline(latency_ms=5.0, loss_rate=0.01)
    call_count = {"n": 0}

    def remediate_fn(excluded):
        call_count["n"] += 1
        return None  # no alternate path ever exists

    outcome = verifier.verify_and_retry(
        "inc-1", baseline, remediate_fn, measure_fn=lambda: (5.0, 0.01), sleep_fn=lambda s: None,
    )

    assert outcome.classification == VerificationClassification.FAILED
    assert call_count["n"] == 1  # no point retrying when there's nothing left to try

    incident = db.query_one("SELECT * FROM incidents WHERE incident_id = ?", ("inc-1",))
    assert incident["status"] == "UNRESOLVED"


# -- THE PHASE 14 GATE: forced failure ends UNRESOLVED, not a loop --------

def test_forced_failure_ends_unresolved_not_a_loop(db, verifier):
    """Every single verification attempt is forced to FAIL, and a fresh
    candidate path is always available (never runs out) -- exactly the
    scenario that would spin a naive retry-forever controller
    indefinitely. Confirms: (1) remediate_fn is called EXACTLY
    max_retries+1 times, never more; (2) the incident ends UNRESOLVED,
    not stuck OPEN or endlessly retrying; (3) every attempt is actually
    recorded, not silently dropped."""
    _open_incident(db, "inc-1")
    baseline = Baseline(latency_ms=5.0, loss_rate=0.01)
    call_count = {"n": 0}
    fresh_paths = [[1, 3, 6], [1, 4, 5, 6], [1, 2, 5, 6], [1, 2, 3, 6], [9, 9, 9]]

    def remediate_fn(excluded):
        call_count["n"] += 1
        # A fresh, never-tried path is always available -- if the loop
        # weren't explicitly bounded, this would run forever.
        return fresh_paths[call_count["n"] - 1]

    def always_bad_measure_fn():
        return 500.0, 0.99  # nowhere near baseline, every single time

    outcome = verifier.verify_and_retry(
        "inc-1", baseline, remediate_fn, always_bad_measure_fn, sleep_fn=lambda s: None,
    )

    assert call_count["n"] == MAX_RETRIES + 1, (
        f"expected exactly {MAX_RETRIES + 1} attempts (bounded), got {call_count['n']}"
    )
    assert outcome.classification == VerificationClassification.FAILED

    incident = db.query_one("SELECT * FROM incidents WHERE incident_id = ?", ("inc-1",))
    assert incident["status"] == "UNRESOLVED"
    assert incident["closed_at"] is not None

    rows = db.query("SELECT * FROM verification_results WHERE incident_id = ? ORDER BY retry_count", ("inc-1",))
    assert len(rows) == MAX_RETRIES + 1
    assert all(r["classification"] == "FAILED" for r in rows)
    assert [r["retry_count"] for r in rows] == list(range(MAX_RETRIES + 1))
