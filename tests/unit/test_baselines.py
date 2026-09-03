import networkx as nx
import pytest

from controller.baselines.rule_based import RuleBasedBaseline, evaluate_current_conditions
from controller.baselines.shortest_path import ShortestPathBaseline, compute_shortest_path
from controller.path_computer import LinkMetrics

SCENARIO_A_EDGES = [(1, 2), (2, 3), (3, 6), (1, 4), (4, 5), (5, 6), (1, 3), (2, 5)]


def _scenario_a_graph() -> nx.Graph:
    graph = nx.Graph()
    graph.add_nodes_from(range(1, 7))
    graph.add_edges_from(SCENARIO_A_EDGES)
    return graph


UTIL_THRESHOLD = 0.8
LOSS_THRESHOLD = 0.1
WEIGHTS = dict(weight_utilization=10.0, weight_latency=1.0, weight_loss=2.0, weight_hop_count=0.1)


# -- B1: shortest_path -----------------------------------------------------

def test_shortest_path_picks_the_shortest_hop_count():
    graph = _scenario_a_graph()
    path = compute_shortest_path(graph, 1, 6)
    assert path is not None
    assert len(path) - 1 == 2  # s1-s3-s6 is the true shortest (2 hops)


def test_shortest_path_no_path_returns_none():
    graph = nx.Graph()
    graph.add_nodes_from([1, 2])
    assert compute_shortest_path(graph, 1, 2) is None


def test_shortest_path_baseline_never_reroutes_for_congestion():
    """B1 has no concept of link metrics at all -- it must stick with the
    same path regardless of how congested it gets, since that's exactly
    what "no self-healing" means for the comparison study."""
    baseline = ShortestPathBaseline()
    graph = _scenario_a_graph()

    first = baseline.get_or_compute_path("h1-h6", graph, 1, 6)
    # Call again and again -- nothing in this baseline's API even accepts
    # link metrics, so there is no way for congestion to influence it.
    second = baseline.get_or_compute_path("h1-h6", graph, 1, 6)
    third = baseline.get_or_compute_path("h1-h6", graph, 1, 6)

    assert first == second == third


def test_shortest_path_baseline_recomputes_when_path_actually_breaks():
    baseline = ShortestPathBaseline()
    graph = _scenario_a_graph()

    original = baseline.get_or_compute_path("h1-h6", graph, 1, 6)
    assert original == [1, 3, 6]

    # A real failure: s1-s3 goes down (removed from the discovered graph,
    # matching how topology_manager.py would reflect an actual link
    # failure), not just "degraded."
    graph.remove_edge(1, 3)

    new_path = baseline.get_or_compute_path("h1-h6", graph, 1, 6)
    assert new_path is not None
    assert new_path != original
    assert all(graph.has_edge(u, v) for u, v in zip(new_path, new_path[1:]))


def test_shortest_path_baseline_returns_none_when_totally_disconnected():
    baseline = ShortestPathBaseline()
    graph = nx.Graph()
    graph.add_nodes_from([1, 6])
    assert baseline.get_or_compute_path("h1-h6", graph, 1, 6) is None


# -- B2: rule_based ----------------------------------------------------------

def test_rule_based_initial_selection_uses_weighted_cost():
    baseline = RuleBasedBaseline(k_shortest=4, utilization_threshold=UTIL_THRESHOLD, loss_threshold=LOSS_THRESHOLD, **WEIGHTS)
    graph = _scenario_a_graph()
    path, decision = baseline.get_or_compute_path("h1-h6", graph, 1, 6, link_metrics={})
    assert path is not None
    assert decision.should_reroute is False


def test_rule_based_no_reroute_when_healthy():
    baseline = RuleBasedBaseline(k_shortest=4, utilization_threshold=UTIL_THRESHOLD, loss_threshold=LOSS_THRESHOLD, **WEIGHTS)
    graph = _scenario_a_graph()
    first, _ = baseline.get_or_compute_path("h1-h6", graph, 1, 6, link_metrics={})

    healthy_metrics = {"s1-s3": LinkMetrics(utilization=0.1, loss_rate=0.0), "s3-s6": LinkMetrics(utilization=0.1, loss_rate=0.0)}
    second, decision = baseline.get_or_compute_path("h1-h6", graph, 1, 6, link_metrics=healthy_metrics)

    assert second == first
    assert decision.should_reroute is False


def test_rule_based_reroutes_when_current_utilization_exceeds_threshold():
    baseline = RuleBasedBaseline(k_shortest=4, utilization_threshold=UTIL_THRESHOLD, loss_threshold=LOSS_THRESHOLD, **WEIGHTS)
    graph = _scenario_a_graph()
    first, _ = baseline.get_or_compute_path("h1-h6", graph, 1, 6, link_metrics={})
    assert first == [1, 3, 6]

    congested = {"s1-s3": LinkMetrics(utilization=0.95), "s3-s6": LinkMetrics(utilization=0.1)}
    second, decision = baseline.get_or_compute_path("h1-h6", graph, 1, 6, link_metrics=congested)

    assert decision.should_reroute is True
    assert second != first


def test_rule_based_reroutes_when_current_loss_exceeds_threshold():
    baseline = RuleBasedBaseline(k_shortest=4, utilization_threshold=UTIL_THRESHOLD, loss_threshold=LOSS_THRESHOLD, **WEIGHTS)
    graph = _scenario_a_graph()
    first, _ = baseline.get_or_compute_path("h1-h6", graph, 1, 6, link_metrics={})

    lossy = {"s1-s3": LinkMetrics(loss_rate=0.30), "s3-s6": LinkMetrics(loss_rate=0.0)}
    second, decision = baseline.get_or_compute_path("h1-h6", graph, 1, 6, link_metrics=lossy)

    assert decision.should_reroute is True
    assert second != first


def test_rule_based_stays_put_if_no_better_alternative_exists():
    baseline = RuleBasedBaseline(k_shortest=4, utilization_threshold=UTIL_THRESHOLD, loss_threshold=LOSS_THRESHOLD, **WEIGHTS)
    # Only one path exists at all -- degraded or not, there's nothing else to pick.
    graph = nx.Graph()
    graph.add_edge(1, 3, ports={})
    graph.add_edge(3, 6, ports={})
    first, _ = baseline.get_or_compute_path("h1-h6", graph, 1, 6, link_metrics={})

    congested = {"s1-s3": LinkMetrics(utilization=0.99)}
    second, decision = baseline.get_or_compute_path("h1-h6", graph, 1, 6, link_metrics=congested)

    assert decision.should_reroute is True  # the rule DID fire
    assert second == first  # but there was nowhere else to go


def test_evaluate_current_conditions_ignores_links_with_no_metrics():
    decision = evaluate_current_conditions([1, 3, 6], link_metrics={}, utilization_threshold=UTIL_THRESHOLD, loss_threshold=LOSS_THRESHOLD)
    assert decision.should_reroute is False


# -- Phase 15 gate: both baselines run end to end on the SAME scenario ----

def test_both_baselines_run_end_to_end_on_the_same_scenario():
    """Runs both B1 and B2 through the identical sequence of network
    conditions (healthy -> congested -> healthy again) on the same
    scenario_a topology, and confirms they behave AS DESIGNED and
    DIFFERENTLY from each other -- this is the actual point of having two
    baselines in the comparison study, not just that neither crashes."""
    graph = _scenario_a_graph()

    b1 = ShortestPathBaseline()
    b2 = RuleBasedBaseline(k_shortest=4, utilization_threshold=UTIL_THRESHOLD, loss_threshold=LOSS_THRESHOLD, **WEIGHTS)

    healthy = {}
    congested = {"s1-s3": LinkMetrics(utilization=0.95), "s3-s6": LinkMetrics(utilization=0.1)}

    # Step 1: healthy.
    b1_path_1 = b1.get_or_compute_path("h1-h6", graph, 1, 6)
    b2_path_1, _ = b2.get_or_compute_path("h1-h6", graph, 1, 6, healthy)

    # Step 2: s1-s3 congested.
    b1_path_2 = b1.get_or_compute_path("h1-h6", graph, 1, 6)
    b2_path_2, b2_decision_2 = b2.get_or_compute_path("h1-h6", graph, 1, 6, congested)

    # Step 3: healthy again.
    b1_path_3 = b1.get_or_compute_path("h1-h6", graph, 1, 6)
    b2_path_3, _ = b2.get_or_compute_path("h1-h6", graph, 1, 6, healthy)

    # B1: static, never reacts to any of this.
    assert b1_path_1 == b1_path_2 == b1_path_3

    # B2: reacted to the congestion (step 2), and -- since B2 only checks
    # conditions on state transitions, not continuously -- stays on
    # whatever it picked once healthy again (no oscillation back on its
    # own, matching "reactive", not "proactive").
    assert b2_decision_2.should_reroute is True
    assert b2_path_2 != b2_path_1
    assert b2_path_3 == b2_path_2

    # Both completed without error and produced real, valid paths at
    # every step -- the actual Phase 15 gate.
    for path in (b1_path_1, b1_path_2, b1_path_3, b2_path_1, b2_path_2, b2_path_3):
        assert path is not None
        assert path[0] == 1 and path[-1] == 6
