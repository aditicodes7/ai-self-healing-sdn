"""Tracks each link's recent P(degradation) history and determines
whether a degradation signal is real/persistent (not a one-off blip) or
falls inside the uncertainty abstention band.

Implements items 1-3 of the reroute decision logic (the project's
DECISION LOGIC spec):
  1. P(degradation) >= ml.threshold (the latest reading)
  2. that has held for persistence.consecutive of the last
     persistence.window readings -- a COUNT within the window, not
     necessarily strictly adjacent readings: "3 of the last 4" is the
     literal spec wording, and since the window always includes the
     latest reading, checking latest >= threshold together with
     count-in-window >= consecutive captures both 1 and 2 at once.
  3. the latest reading is NOT inside persistence.uncertainty_band; if it
     is, the correct action is explicit abstention ("uncertain"), not a
     coin-flip toward action or inaction.

Items 4-6 (cooldown, min-improvement, route-hold) are a SEPARATE concern
-- controller/stability_manager.py -- because they're about route-change
session state, not about how to interpret the ML signal itself.

Pure Python, no framework dependency: testable from any venv.
"""
from __future__ import annotations

import collections
import dataclasses


@dataclasses.dataclass(frozen=True)
class PersistenceResult:
    link_id: str
    latest_probability: float
    persistent_degradation: bool
    uncertain: bool
    consecutive_count: int
    window_size: int
    reason: str


class PersistenceTracker:
    def __init__(
        self, window: int, consecutive: int, uncertainty_band: tuple[float, float], threshold: float,
    ):
        if consecutive > window:
            raise ValueError(
                f"persistence.consecutive ({consecutive}) cannot exceed persistence.window ({window})"
            )
        self.window = window
        self.consecutive = consecutive
        self.uncertainty_low, self.uncertainty_high = uncertainty_band
        self.threshold = threshold
        self._history: dict[str, collections.deque] = {}

    def record(self, link_id: str, probability: float) -> None:
        history = self._history.setdefault(link_id, collections.deque(maxlen=self.window))
        history.append(probability)

    def evaluate(self, link_id: str) -> PersistenceResult:
        history = self._history.get(link_id)
        if not history:
            return PersistenceResult(link_id, 0.0, False, False, 0, 0, "no readings recorded yet")

        latest = history[-1]

        if self.uncertainty_low < latest < self.uncertainty_high:
            return PersistenceResult(
                link_id, latest, False, True, 0, len(history),
                f"uncertain: probability {latest:.3f} inside band "
                f"[{self.uncertainty_low}, {self.uncertainty_high}]",
            )

        count_above = sum(1 for p in history if p >= self.threshold)
        persistent = latest >= self.threshold and count_above >= self.consecutive

        if persistent:
            reason = (
                f"persistence confirmed {count_above} of {len(history)} "
                f"windows >= threshold {self.threshold}"
            )
        else:
            reason = (
                f"not persistent: {count_above} of {len(history)} windows "
                f">= threshold {self.threshold} (need {self.consecutive})"
            )

        return PersistenceResult(link_id, latest, persistent, False, count_above, len(history), reason)
