import itertools

import networkx as nx
import pytest

from controller.path_computer import (
    LinkMetrics,
    compute_path_cost,
    generate_candidate_paths,
    rank_candidate_paths,
)

# Mirrors network/topologies/scenario_a.yaml exactly: dpid N <-> switch sN.
SCENARIO_A_EDGES = [
    (1, 2), (2, 3), (3, 6), (1, 4), (4, 5), (5, 6), (1, 3), (2, 5),
]


def _scenario_a_graph() -> nx.Graph:
    graph = nx.Graph()
    graph.add_nodes_from(range(1, 7))
    graph.add_edges_from(SCENARIO_A_EDGES)
    return graph


# -- generate_candidate_paths ----------------------------------------------

def test_candidates_exist_for_every_switch_pair():
    graph = _scenario_a_graph()
    for src, dst in itertools.permutations(range(1, 7), 2):
        candidates = generate_candidate_paths(graph, src, dst, k=4)
        assert len(candidates) >= 1, f"no candidate path found for {src}->{dst}"
        for path in candidates:
            assert path[0] == src
            assert path[-1] == dst
            assert len(set(path)) == len(path), f"path {path} is not simple (loopless)"


def test_s1_to_s6_has_at_least_four_loop_free_paths():
    # network/topologies/scenario_a.yaml was explicitly designed so h1->h6
    # (s1->s6) has >=3 loop-free paths of differing cost (Phase 3). Confirm
    # the path computer actually finds them, not just that the topology
    # supports them in principle.
    graph = _scenario_a_graph()
    candidates = generate_candidate_paths(graph, 1, 6, k=4)
    assert len(candidates) == 4
    # All candidates distinct.
    assert len({tuple(p) for p in candidates}) == 4
    # Shortest (by hop count) comes first.
    assert len(candidates[0]) <= len(candidates[-1])


def test_candidates_respect_k_limit():
    graph = _scenario_a_graph()
    candidates = generate_candidate_paths(graph, 1, 6, k=2)
    assert len(candidates) == 2


def test_no_path_between_src_and_dst_returns_empty():
    graph = nx.Graph()
    graph.add_nodes_from([1, 2, 3])
    graph.add_edge(1, 2)  # 3 is isolated
    assert generate_candidate_paths(graph, 1, 3, k=4) == []


def test_src_equals_dst_returns_empty():
    graph = _scenario_a_graph()
    assert generate_candidate_paths(graph, 1, 1, k=4) == []


def test_unknown_node_returns_empty_not_a_crash():
    graph = _scenario_a_graph()
    assert generate_candidate_paths(graph, 1, 99, k=4) == []


# -- compute_path_cost ------------------------------------------------------

def test_cost_uses_max_utilization_not_sum():
    # Path s1-s2-s3: utilization 0.2 and 0.8 -> bottleneck is 0.8, not 1.0.
    metrics = {
        "s1-s2": LinkMetrics(utilization=0.2),
        "s2-s3": LinkMetrics(utilization=0.8),
    }
    cost = compute_path_cost(
        [1, 2, 3], metrics,
        weight_utilization=1.0, weight_latency=0.0, weight_loss=0.0, weight_hop_count=0.0,
    )
    assert cost == pytest.approx(0.8)


def test_cost_sums_latency_across_hops():
    metrics = {
        "s1-s2": LinkMetrics(latency_ms=2.0),
        "s2-s3": LinkMetrics(latency_ms=3.0),
    }
    cost = compute_path_cost(
        [1, 2, 3], metrics,
        weight_utilization=0.0, weight_latency=1.0, weight_loss=0.0, weight_hop_count=0.0,
    )
    assert cost == pytest.approx(5.0)


def test_cost_uses_max_loss_not_sum():
    metrics = {
        "s1-s2": LinkMetrics(loss_rate=0.01),
        "s2-s3": LinkMetrics(loss_rate=0.10),
    }
    cost = compute_path_cost(
        [1, 2, 3], metrics,
        weight_utilization=0.0, weight_latency=0.0, weight_loss=1.0, weight_hop_count=0.0,
    )
    assert cost == pytest.approx(0.10)


def test_cost_includes_hop_count():
    cost = compute_path_cost(
        [1, 2, 3, 6], {},
        weight_utilization=0.0, weight_latency=0.0, weight_loss=0.0, weight_hop_count=1.0,
    )
    assert cost == pytest.approx(3.0)  # 3 hops


def test_missing_link_metrics_default_to_zero_contribution():
    # No entry at all for "s1-s2" -- explicit "assume healthy" default,
    # not a crash and not treated as maximally bad.
    cost = compute_path_cost(
        [1, 2], {},
        weight_utilization=1.0, weight_latency=1.0, weight_loss=1.0, weight_hop_count=0.0,
    )
    assert cost == 0.0


def test_single_node_path_has_zero_cost():
    assert compute_path_cost([1], {}, 1.0, 1.0, 1.0, 1.0) == 0.0


# -- rank_candidate_paths -----------------------------------------------------

def test_ranking_sorts_by_ascending_cost():
    graph = _scenario_a_graph()
    # Make the direct-ish s1-s3-s6 path expensive via high utilization,
    # so a longer but less-congested path should rank first instead.
    metrics = {
        "s1-s3": LinkMetrics(utilization=0.95),
        "s3-s6": LinkMetrics(utilization=0.95),
        "s1-s2": LinkMetrics(utilization=0.1),
        "s2-s3": LinkMetrics(utilization=0.1),
    }
    ranked = rank_candidate_paths(
        graph, 1, 6, k=4, link_metrics=metrics,
        weight_utilization=10.0, weight_latency=0.0, weight_loss=0.0, weight_hop_count=0.1,
    )
    assert len(ranked) == 4
    costs = [cost for _, cost in ranked]
    assert costs == sorted(costs)
    # The heavily-congested [1,3,6] path must not be ranked first.
    assert ranked[0][0] != [1, 3, 6]


def test_ranking_empty_when_no_candidates():
    graph = nx.Graph()
    graph.add_nodes_from([1, 2])
    assert rank_candidate_paths(graph, 1, 2, k=4, link_metrics={}, weight_utilization=1, weight_latency=1, weight_loss=1, weight_hop_count=1) == []
