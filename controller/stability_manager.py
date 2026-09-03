"""Final reroute gate: combines persistence_tracker's read of the ML
signal with route-change session state (cooldown, minimum improvement,
route hold) to decide whether to actually change a flow's path.
Implements items 4-6 of the DECISION LOGIC spec:
  4. now - last_change[flow] > stability.cooldown_s
  5. predicted improvement over the current path exceeds
     stability.min_improvement (a fraction of the current path's
     weighted cost)
  6. the flow is outside its stability.route_hold_s window

evaluate() returns a Decision for every call, including negatives -- it
never raises or skips silently, so a caller that logs every returned
Decision satisfies the spec's "log EVERY decision including the
negatives" requirement.

Flap prevention (Experiment 7 in the phase plan) comes from checking
BOTH cooldown_s and route_hold_s against the same last-change timestamp:
even a flow that newly re-qualifies (fresh persistent-degradation signal,
real improvement available) is refused a second change while still
inside either window. This is what stops a path from oscillating back
and forth every time a borderline signal wobbles across the threshold.
"""
from __future__ import annotations

import dataclasses
import time


@dataclasses.dataclass(frozen=True)
class Decision:
    change: bool
    reason: str


class StabilityManager:
    def __init__(self, cooldown_s: float, route_hold_s: float, min_improvement: float):
        self.cooldown_s = cooldown_s
        self.route_hold_s = route_hold_s
        self.min_improvement = min_improvement
        self._last_change_ts: dict[str, float] = {}

    def evaluate(
        self,
        flow_key: str,
        persistent_degradation: bool,
        uncertain: bool,
        persistence_reason: str,
        current_path_cost: float,
        best_candidate_cost: float,
        now: float | None = None,
    ) -> Decision:
        """`now` is injectable for deterministic tests; live callers leave
        it as None and get time.time()."""
        now = now if now is not None else time.time()

        if uncertain:
            return Decision(False, persistence_reason)
        if not persistent_degradation:
            return Decision(False, persistence_reason)

        last_change = self._last_change_ts.get(flow_key)
        if last_change is not None:
            elapsed = now - last_change
            if elapsed <= self.cooldown_s:
                return Decision(
                    False,
                    f"cooldown active: {elapsed:.1f}s of {self.cooldown_s}s since last change on {flow_key}",
                )
            if elapsed <= self.route_hold_s:
                return Decision(
                    False,
                    f"route hold active: {elapsed:.1f}s of {self.route_hold_s}s since last change on {flow_key}",
                )

        improvement = 0.0 if current_path_cost <= 0 else (current_path_cost - best_candidate_cost) / current_path_cost

        if improvement < self.min_improvement:
            return Decision(
                False,
                f"insufficient improvement: {improvement:.1%} < required {self.min_improvement:.1%}",
            )

        self._last_change_ts[flow_key] = now
        return Decision(
            True,
            f"reroute: persistence confirmed, improvement {improvement:.1%} >= {self.min_improvement:.1%}",
        )
