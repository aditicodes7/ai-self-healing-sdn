"""Candidate-path generation and weighted-cost ranking over the discovered
topology graph (controller/topology_manager.py's networkx.Graph).

Two-step design, deliberately kept separate:
  1. generate_candidate_paths() -- pure topology structure. Up to
     path.k_shortest simple (loopless) paths between src and dst, in
     increasing hop-count order, via nx.shortest_simple_paths (per the
     approved stack: "Graph/paths: NetworkX"). Independent of current
     traffic conditions, so candidates stay stable even while telemetry
     is noisy.
  2. compute_path_cost() -- weighted cost of ONE candidate path, given
     current per-link metrics (utilization/latency/loss from live
     telemetry or predictions) and path.weights (config.yaml). Kept
     separate from candidate generation so the two are independently
     testable and so a path's cost can be recomputed as conditions change
     without regenerating candidates.

Pure Python + networkx -- no ryu/mininet dependency, importable and
testable from any venv.
"""
from __future__ import annotations

import dataclasses

import networkx as nx


@dataclasses.dataclass(frozen=True)
class LinkMetrics:
    """Current known metrics for one link. Any field may be None --
    "not yet measured", never silently treated as 0 by the caller without
    that being an explicit, documented choice (see compute_path_cost)."""

    utilization: float | None = None
    latency_ms: float | None = None
    loss_rate: float | None = None


def generate_candidate_paths(
    graph: nx.Graph, src: int, dst: int, k: int
) -> list[list[int]]:
    """Up to k simple paths from src to dst, shortest (by hop count) first.
    Returns fewer than k if fewer simple paths exist -- callers (e.g.
    Phase 16's RL state) are responsible for any fixed-size padding they
    need; this function never fabricates a path that doesn't exist.
    Returns [] if src == dst or no path exists at all.
    """
    if src == dst:
        return []
    if src not in graph or dst not in graph:
        return []
    if not nx.has_path(graph, src, dst):
        return []

    paths: list[list[int]] = []
    for path in nx.shortest_simple_paths(graph, src, dst):
        paths.append(path)
        if len(paths) >= k:
            break
    return paths


def _link_id_for_edge(u: int, v: int) -> str:
    a, b = sorted((f"s{u}", f"s{v}"))
    return f"{a}-{b}"


def compute_path_cost(
    path: list[int],
    link_metrics: dict[str, LinkMetrics],
    weight_utilization: float,
    weight_latency: float,
    weight_loss: float,
    weight_hop_count: float,
) -> float:
    """Weighted cost of one path: higher is worse. Combines the MAX
    utilization and MAX loss along the path (a path is only as good as its
    most congested/lossiest hop -- a bottleneck link dominates end-to-end
    behavior, so summing would understate it) with the SUM of latency
    (propagation delay is additive across hops) and the path's hop count.

    A link with no metrics yet (LinkMetrics() default, or missing from
    link_metrics entirely) contributes 0 to utilization/latency/loss --
    an explicit "assume healthy until proven otherwise" default, not a
    silent one: a brand-new or never-faulted link is more likely healthy
    than not, and Phase 12's persistence/stability logic (not this
    function) is what actually gates any reroute decision, so an
    optimistic default here does not by itself trigger unsafe behavior.
    """
    if len(path) < 2:
        return 0.0

    max_utilization = 0.0
    sum_latency = 0.0
    max_loss = 0.0

    for u, v in zip(path, path[1:]):
        metrics = link_metrics.get(_link_id_for_edge(u, v), LinkMetrics())
        if metrics.utilization is not None:
            max_utilization = max(max_utilization, metrics.utilization)
        if metrics.latency_ms is not None:
            sum_latency += metrics.latency_ms
        if metrics.loss_rate is not None:
            max_loss = max(max_loss, metrics.loss_rate)

    hop_count = len(path) - 1

    return (
        weight_utilization * max_utilization
        + weight_latency * sum_latency
        + weight_loss * max_loss
        + weight_hop_count * hop_count
    )


def rank_candidate_paths(
    graph: nx.Graph,
    src: int,
    dst: int,
    k: int,
    link_metrics: dict[str, LinkMetrics],
    weight_utilization: float,
    weight_latency: float,
    weight_loss: float,
    weight_hop_count: float,
) -> list[tuple[list[int], float]]:
    """generate_candidate_paths() + compute_path_cost() for each, sorted by
    ascending cost (best first). Ties broken by path order from
    generate_candidate_paths (i.e. shorter hop count first), via Python's
    stable sort."""
    candidates = generate_candidate_paths(graph, src, dst, k)
    scored = [
        (path, compute_path_cost(
            path, link_metrics, weight_utilization, weight_latency, weight_loss, weight_hop_count,
        ))
        for path in candidates
    ]
    scored.sort(key=lambda pair: pair[1])
    return scored
