"""B2 baseline: rule-based reactive self-healing, for the Phase 22
comparison study. Reroutes when CURRENT observed utilization or loss on
the active path exceeds a fixed threshold -- no ML prediction, no
persistence/lead-time anticipation, just an immediate reactive rule on
already-degraded conditions. This is what isolates the actual value of
prediction + persistence (P1) from simple threshold-based reaction: B2
can only ever detect a link that has ALREADY crossed the line, never one
that's about to.
"""
from __future__ import annotations

import dataclasses

import networkx as nx

from controller.path_computer import LinkMetrics, link_id_for_edge, rank_candidate_paths


@dataclasses.dataclass(frozen=True)
class RuleBasedDecision:
    should_reroute: bool
    reason: str


def evaluate_current_conditions(
    path: list[int],
    link_metrics: dict[str, LinkMetrics],
    utilization_threshold: float,
    loss_threshold: float,
) -> RuleBasedDecision:
    """Checks the CURRENT metrics on every link of `path` (not predicted,
    not persisted across windows); reroutes if any link exceeds either
    threshold right now. A link with no metrics yet is skipped, same
    "assume healthy until proven otherwise" default as path_computer.py."""
    for u, v in zip(path, path[1:]):
        metrics = link_metrics.get(link_id_for_edge(u, v))
        if metrics is None:
            continue
        if metrics.utilization is not None and metrics.utilization > utilization_threshold:
            return RuleBasedDecision(
                True, f"{link_id_for_edge(u, v)} utilization {metrics.utilization:.2f} > {utilization_threshold}"
            )
        if metrics.loss_rate is not None and metrics.loss_rate > loss_threshold:
            return RuleBasedDecision(
                True, f"{link_id_for_edge(u, v)} loss_rate {metrics.loss_rate:.3f} > {loss_threshold}"
            )
    return RuleBasedDecision(False, "all links within thresholds")


class RuleBasedBaseline:
    def __init__(
        self, k_shortest: int, utilization_threshold: float, loss_threshold: float,
        weight_utilization: float, weight_latency: float, weight_loss: float, weight_hop_count: float,
    ):
        self.k_shortest = k_shortest
        self.utilization_threshold = utilization_threshold
        self.loss_threshold = loss_threshold
        self.weight_utilization = weight_utilization
        self.weight_latency = weight_latency
        self.weight_loss = weight_loss
        self.weight_hop_count = weight_hop_count
        self._active_paths: dict[str, list[int]] = {}

    def get_or_compute_path(
        self, flow_key: str, graph: nx.Graph, src: int, dst: int, link_metrics: dict[str, LinkMetrics],
    ) -> tuple[list[int] | None, RuleBasedDecision]:
        current = self._active_paths.get(flow_key)

        if current is None:
            ranked = rank_candidate_paths(
                graph, src, dst, self.k_shortest, link_metrics,
                self.weight_utilization, self.weight_latency, self.weight_loss, self.weight_hop_count,
            )
            if not ranked:
                return None, RuleBasedDecision(False, "no path available")
            chosen = ranked[0][0]
            self._active_paths[flow_key] = chosen
            return chosen, RuleBasedDecision(False, "initial path selection")

        decision = evaluate_current_conditions(current, link_metrics, self.utilization_threshold, self.loss_threshold)
        if not decision.should_reroute:
            return current, decision

        ranked = rank_candidate_paths(
            graph, src, dst, self.k_shortest, link_metrics,
            self.weight_utilization, self.weight_latency, self.weight_loss, self.weight_hop_count,
        )
        candidates = [(p, c) for p, c in ranked if p != current]
        if not candidates:
            return current, decision  # nothing better available, stay put

        chosen = candidates[0][0]
        self._active_paths[flow_key] = chosen
        return chosen, decision
