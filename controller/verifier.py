"""Post-remediation verification + bounded retry.

Ties together remediation_manager.remediate() with a
settle-then-measure-then-classify loop: after flows are installed, wait
verify.settle_s, collect verify.windows fresh telemetry samples, and
classify the outcome against a pre-remediation baseline (SUCCESS if loss
and latency are back inside baseline + tolerance, PARTIAL if only one of
the two is, FAILED otherwise). On FAILED, retries with the next candidate
path (excluding every path already tried for this incident) up to
remediation.max_retries times, THEN marks the incident UNRESOLVED and
STOPS. The retry loop is explicitly bounded -- max_retries+1 total
attempts, never open-ended -- so a fault this system genuinely cannot fix
surfaces as a clear terminal state instead of spinning forever.

remediate_fn/measure_fn/sleep_fn are injected so verify_and_retry() is
fully testable without a live network or real time delays (see
tests/unit/test_verifier.py's forced-failure test, Phase 14's gate).
"""
from __future__ import annotations

import dataclasses
import json
import time
from typing import Callable

from common.logging_setup import configure_logging
from common.schema import IncidentStatus, VerificationClassification
from database.db import Database

logger = configure_logging("controller.verifier")


@dataclasses.dataclass(frozen=True)
class Baseline:
    latency_ms: float
    loss_rate: float


@dataclasses.dataclass(frozen=True)
class VerificationOutcome:
    classification: VerificationClassification
    post_latency_ms: float | None
    post_loss_rate: float | None
    reason: str


def _within_tolerance(post: float, baseline: float, tol_fraction: float) -> bool:
    """post is acceptable if it's within tol_fraction of baseline,
    relative to baseline. If baseline itself is ~0 (a healthy metric with
    no headroom to be relative to -- e.g. baseline loss_rate=0.0), falls
    back to treating tol_fraction as an absolute ceiling instead: with
    tol_fraction=0.20, post<=0.20 is acceptable, not "must be exactly 0"."""
    if baseline > 1e-9:
        return post <= baseline * (1 + tol_fraction)
    return post <= tol_fraction


def classify(
    baseline: Baseline,
    post_latency_ms: float | None,
    post_loss_rate: float | None,
    latency_tolerance: float,
    loss_tolerance: float,
) -> VerificationOutcome:
    if post_latency_ms is None or post_loss_rate is None:
        return VerificationOutcome(
            VerificationClassification.FAILED, post_latency_ms, post_loss_rate,
            "no post-remediation telemetry available",
        )

    latency_ok = _within_tolerance(post_latency_ms, baseline.latency_ms, latency_tolerance)
    loss_ok = _within_tolerance(post_loss_rate, baseline.loss_rate, loss_tolerance)

    if latency_ok and loss_ok:
        return VerificationOutcome(
            VerificationClassification.SUCCESS, post_latency_ms, post_loss_rate,
            "within baseline + tolerance on both metrics",
        )
    if latency_ok or loss_ok:
        return VerificationOutcome(
            VerificationClassification.PARTIAL, post_latency_ms, post_loss_rate,
            "within tolerance on only one metric",
        )
    return VerificationOutcome(
        VerificationClassification.FAILED, post_latency_ms, post_loss_rate,
        "outside tolerance on both metrics",
    )


class Verifier:
    def __init__(
        self, db: Database, settle_s: float, windows: int,
        latency_tolerance: float, loss_tolerance: float, max_retries: int,
    ):
        self.db = db
        self.settle_s = settle_s
        self.windows = windows
        self.latency_tolerance = latency_tolerance
        self.loss_tolerance = loss_tolerance
        self.max_retries = max_retries

    def verify_and_retry(
        self,
        incident_id: str,
        baseline: Baseline,
        remediate_fn: Callable[[list[list[int]]], list[int] | None],
        measure_fn: Callable[[], tuple[float | None, float | None]],
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> VerificationOutcome:
        """remediate_fn(excluded_paths) -> chosen_path or None (no
        alternate left). measure_fn() -> (post_latency_ms, post_loss_rate)
        for the just-installed path, called after settling."""
        excluded_paths: list[list[int]] = []
        outcome: VerificationOutcome | None = None

        for attempt in range(self.max_retries + 1):
            chosen_path = remediate_fn(excluded_paths)
            if chosen_path is None:
                outcome = VerificationOutcome(
                    VerificationClassification.FAILED, None, None, "no alternate path available",
                )
                self._record_verification(incident_id, None, outcome, baseline, attempt)
                break

            excluded_paths.append(chosen_path)
            sleep_fn(self.settle_s)
            post_latency_ms, post_loss_rate = measure_fn()
            outcome = classify(
                baseline, post_latency_ms, post_loss_rate, self.latency_tolerance, self.loss_tolerance,
            )
            self._record_verification(incident_id, chosen_path, outcome, baseline, attempt)

            if outcome.classification != VerificationClassification.FAILED:
                status = (
                    IncidentStatus.SUCCESS if outcome.classification == VerificationClassification.SUCCESS
                    else IncidentStatus.PARTIAL
                )
                self._close_incident(incident_id, status)
                logger.info(
                    "verification concluded",
                    extra={"incident_id": incident_id, "classification": outcome.classification.value, "attempt": attempt},
                )
                return outcome

            remaining = self.max_retries - attempt
            logger.warning(
                "verification FAILED" + (f", {remaining} retries left" if remaining > 0 else ", retries exhausted"),
                extra={"incident_id": incident_id, "attempt": attempt, "path": chosen_path, "reason": outcome.reason},
            )

        # Bounded retry exhausted (or no alternate path ever existed) --
        # STOP here. Never loop forever on a fault this system can't fix.
        self._close_incident(incident_id, IncidentStatus.UNRESOLVED)
        logger.error("incident UNRESOLVED after exhausting retries", extra={"incident_id": incident_id})
        return outcome

    def _record_verification(
        self, incident_id: str, path: list[int] | None, outcome: VerificationOutcome,
        baseline: Baseline, retry_count: int,
    ) -> None:
        self.db.insert(
            "verification_results",
            {
                "incident_id": incident_id,
                "timestamp": time.time(),
                "path": ",".join(f"s{d}" for d in path) if path else "",
                "classification": outcome.classification.value,
                "baseline_json": json.dumps(dataclasses.asdict(baseline)),
                "post_json": json.dumps(
                    {"latency_ms": outcome.post_latency_ms, "loss_rate": outcome.post_loss_rate, "reason": outcome.reason}
                ),
                "retry_count": retry_count,
            },
        )

    def _close_incident(self, incident_id: str, status: IncidentStatus) -> None:
        self.db.execute(
            "UPDATE incidents SET status = ?, closed_at = ? WHERE incident_id = ?",
            (status.value, time.time(), incident_id),
        )
