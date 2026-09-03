"""Reward computation for the RL route-selection environment, per the
spec's weighted terms (config.yaml's rl.reward_weights):
  + w1*delta_throughput + w2*(-delta_latency) + w3*(-delta_loss)
  + w4*recovery_success
  - w5*max_utilization_after - w6*reroute_cost (if path changed)
  - w7*oscillation_penalty (if reverted within hold window)
  - w8*failed_remediation

All terms are measured from the simulator step (rl/env/simulator.py),
never fabricated: delta_* come from real before/after NetworkSimulator
flow metrics, max_utilization_after comes from the real post-action link
states, and the boolean flags (recovery_success/failed_remediation/
oscillation) are computed from those same real values, not guessed.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class RewardWeights:
    w1_throughput: float
    w2_latency: float
    w3_loss: float
    w4_recovery_success: float
    w5_max_utilization_after: float
    w6_reroute_cost: float
    w7_oscillation_penalty: float
    w8_failed_remediation: float


@dataclasses.dataclass(frozen=True)
class RewardComponents:
    """Every term, kept alongside the total -- useful for debugging a
    learning curve and for the technical paper's methodology section,
    not just the scalar reward itself."""

    delta_throughput_term: float
    delta_latency_term: float
    delta_loss_term: float
    recovery_success_term: float
    max_utilization_term: float
    reroute_cost_term: float
    oscillation_term: float
    failed_remediation_term: float

    @property
    def total(self) -> float:
        return (
            self.delta_throughput_term
            + self.delta_latency_term
            + self.delta_loss_term
            + self.recovery_success_term
            - self.max_utilization_term
            - self.reroute_cost_term
            - self.oscillation_term
            - self.failed_remediation_term
        )


def compute_reward(
    weights: RewardWeights,
    delta_throughput_bps: float,
    delta_latency_ms: float,
    delta_loss_rate: float,
    recovery_success: bool,
    max_utilization_after: float,
    path_changed: bool,
    reverted_within_hold_window: bool,
    remediation_failed: bool,
    throughput_scale_bps: float = 1_000_000.0,
    latency_scale_ms: float = 10.0,
) -> RewardComponents:
    """throughput_scale_bps/latency_scale_ms normalize raw simulator units
    (bps, ms) to an O(1) range before applying the configured weights --
    without this, a multi-Mbit delta_throughput would dominate every
    other term purely from unit choice, not from the weights actually
    expressing that priority."""
    return RewardComponents(
        delta_throughput_term=weights.w1_throughput * (delta_throughput_bps / throughput_scale_bps),
        delta_latency_term=weights.w2_latency * (-delta_latency_ms / latency_scale_ms),
        delta_loss_term=weights.w3_loss * (-delta_loss_rate),
        recovery_success_term=weights.w4_recovery_success * (1.0 if recovery_success else 0.0),
        max_utilization_term=weights.w5_max_utilization_after * max_utilization_after,
        reroute_cost_term=weights.w6_reroute_cost * (1.0 if path_changed else 0.0),
        oscillation_term=weights.w7_oscillation_penalty * (1.0 if reverted_within_hold_window else 0.0),
        failed_remediation_term=weights.w8_failed_remediation * (1.0 if remediation_failed else 0.0),
    )
