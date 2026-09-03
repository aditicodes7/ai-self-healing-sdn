import time

import networkx as nx
import pytest

from controller.flow_manager import FlowManager
from controller.path_computer import LinkMetrics
from controller.remediation_manager import FlowContext, RemediationManager
from database.db import Database
from tests.unit.test_flow_manager import MockDatapath

SCENARIO_A_EDGES = [
    (1, 2, {"ports": {1: 2, 2: 2}}),
    (2, 3, {"ports": {2: 3, 3: 2}}),
    (3, 6, {"ports": {3: 3, 6: 2}}),
    (1, 4, {"ports": {1: 3, 4: 2}}),
    (4, 5, {"ports": {4: 3, 5: 2}}),
    (5, 6, {"ports": {5: 3, 6: 3}}),
    (1, 3, {"ports": {1: 4, 3: 4}}),
    (2, 5, {"ports": {2: 4, 5: 4}}),
]


def _scenario_a_graph() -> nx.Graph:
    graph = nx.Graph()
    graph.add_nodes_from(range(1, 7))
    for u, v, data in SCENARIO_A_EDGES:
        graph.add_edge(u, v, **data)
    return graph


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
def flow_manager(db):
    return FlowManager(db, priority=200, hard_timeout_s=300, idle_timeout_s=0)


@pytest.fixture
def manager(db, flow_manager):
    return RemediationManager(
        db, flow_manager, k_shortest=4,
        weight_utilization=10.0, weight_latency=1.0, weight_loss=2.0, weight_hop_count=0.1,
    )


@pytest.fixture
def flow_ctx():
    return FlowContext(
        flow_key="h1-h6", src_dpid=1, dst_dpid=6,
        src_host_port=1, dst_host_port=1,
        ipv4_src="10.0.0.1", ipv4_dst="10.0.0.6",
    )


def _all_datapaths():
    return {dpid: MockDatapath(dpid) for dpid in range(1, 7)}


def test_remediate_avoids_the_failing_path(db, manager, flow_ctx):
    _open_incident(db, "inc-1", "s1-s3")
    graph = _scenario_a_graph()
    current_path = [1, 3, 6]  # the path being remediated away from
    # s1-s3 and s3-s6 badly congested -- should push cost of [1,3,6] way up.
    link_metrics = {
        "s1-s3": LinkMetrics(utilization=0.99),
        "s3-s6": LinkMetrics(utilization=0.99),
    }

    chosen_path, results = manager.remediate(
        "inc-1", "s1-s3", flow_ctx, graph, link_metrics, _all_datapaths(), [current_path],
    )

    assert chosen_path is not None
    assert chosen_path != current_path
    assert all(r.result.value == "INSTALLED" for r in results)


def test_remediate_installs_flows_on_every_switch_in_chosen_path(db, manager, flow_ctx):
    _open_incident(db, "inc-1")
    graph = _scenario_a_graph()
    datapaths = _all_datapaths()

    chosen_path, results = manager.remediate(
        "inc-1", "s1-s3", flow_ctx, graph, {}, datapaths, excluded_paths=[[1, 3, 6]],
    )

    assert len(results) == len(chosen_path)
    # Endpoints use the flow's host-facing ports, not a graph lookup.
    installed_dpids = {r.dpid for r in results}
    assert installed_dpids == set(chosen_path)


def test_remediate_resolves_host_facing_ports_at_path_ends(db, manager, flow_ctx):
    _open_incident(db, "inc-1")
    graph = _scenario_a_graph()

    hops = manager._resolve_hops([1, 2, 5, 6], flow_ctx, graph)

    assert hops[0].dpid == 1
    assert hops[0].in_port == flow_ctx.src_host_port  # host-facing, not graph-derived
    assert hops[-1].dpid == 6
    assert hops[-1].out_port == flow_ctx.dst_host_port  # host-facing
    # Middle hops use graph-derived inter-switch ports.
    assert hops[1].in_port == graph[1][2]["ports"][2]
    assert hops[1].out_port == graph[2][5]["ports"][2]


def test_remediate_records_route_change_in_db(db, manager, flow_ctx):
    _open_incident(db, "inc-1")
    graph = _scenario_a_graph()

    chosen_path, _ = manager.remediate(
        "inc-1", "s1-s3", flow_ctx, graph, {}, _all_datapaths(), excluded_paths=[[1, 3, 6]],
    )

    row = db.query_one("SELECT * FROM route_changes WHERE incident_id = ?", ("inc-1",))
    assert row["changed"] == 1
    assert row["selector"] == "P1_ML_HEURISTIC"
    assert row["flow_key"] == "h1-h6"
    assert row["old_path"] == "s1,s3,s6"
    assert row["new_path"] == ",".join(f"s{d}" for d in chosen_path)


class FakeRLDecision:
    """Duck-typed stand-in for controller.rl_client.RouteDecision -- that
    real class can't be imported under venv-ml (it lives in a module that
    imports `requests`, which venv-ml deliberately doesn't install; see
    docs/decisions.md 2026-09-03). RemediationManager only ever reads
    .action/.chosen_path/.policy_version off whatever select_route()
    returns, so a matching plain object is a faithful substitute."""

    def __init__(self, action, chosen_path, policy_version="test-policy"):
        self.action = action
        self.chosen_path = chosen_path
        self.policy_version = policy_version


class FakeRLClient:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def select_route(self, src_dpid, dst_dpid, candidates, link_metrics, degradation_probs,
                      degraded_link_id, current_path, time_since_last_reroute_s,
                      global_mean_utilization, global_max_utilization):
        self.calls.append(
            {
                "src_dpid": src_dpid, "dst_dpid": dst_dpid, "candidates": candidates,
                "current_path": current_path, "degraded_link_id": degraded_link_id,
            }
        )
        return self.decision


@pytest.fixture
def manager_with_rl(db, flow_manager):
    def _make(rl_client):
        return RemediationManager(
            db, flow_manager, k_shortest=4,
            weight_utilization=10.0, weight_latency=1.0, weight_loss=2.0, weight_hop_count=0.1,
            rl_client=rl_client,
        )

    return _make


def test_remediate_uses_rl_selected_path_when_valid(db, manager_with_rl, flow_ctx):
    _open_incident(db, "inc-1", "s1-s3")
    graph = _scenario_a_graph()
    current_path = [1, 3, 6]
    rl_client = FakeRLClient(FakeRLDecision(action=2, chosen_path=[1, 4, 5, 6]))
    manager = manager_with_rl(rl_client)

    chosen_path, results = manager.remediate(
        "inc-1", "s1-s3", flow_ctx, graph, {}, _all_datapaths(), [current_path],
    )

    assert chosen_path == [1, 4, 5, 6]
    assert all(r.result.value == "INSTALLED" for r in results)
    row = db.query_one("SELECT * FROM route_changes WHERE incident_id = ?", ("inc-1",))
    assert row["selector"] == "P2_ML_RL"
    assert row["rl_fallback"] == 0
    assert row["rl_fallback_reason"] is None
    assert len(rl_client.calls) == 1


def test_remediate_falls_back_when_rl_client_returns_none(db, manager_with_rl, flow_ctx):
    _open_incident(db, "inc-1", "s1-s3")
    graph = _scenario_a_graph()
    current_path = [1, 3, 6]
    rl_client = FakeRLClient(None)
    manager = manager_with_rl(rl_client)

    chosen_path, results = manager.remediate(
        "inc-1", "s1-s3", flow_ctx, graph, {}, _all_datapaths(), [current_path],
    )

    assert chosen_path is not None
    row = db.query_one("SELECT * FROM route_changes WHERE incident_id = ?", ("inc-1",))
    assert row["selector"] == "P1_ML_HEURISTIC"
    assert row["rl_fallback"] == 1
    assert row["rl_fallback_reason"] == "rl_service_unavailable"


def test_remediate_falls_back_when_rl_selects_hold(db, manager_with_rl, flow_ctx):
    _open_incident(db, "inc-1", "s1-s3")
    graph = _scenario_a_graph()
    current_path = [1, 3, 6]
    rl_client = FakeRLClient(FakeRLDecision(action=0, chosen_path=current_path))
    manager = manager_with_rl(rl_client)

    chosen_path, results = manager.remediate(
        "inc-1", "s1-s3", flow_ctx, graph, {}, _all_datapaths(), [current_path],
    )

    assert chosen_path is not None
    assert chosen_path != current_path
    row = db.query_one("SELECT * FROM route_changes WHERE incident_id = ?", ("inc-1",))
    assert row["rl_fallback"] == 1
    assert row["rl_fallback_reason"] == "rl_selected_no_reroute"


def test_remediate_falls_back_when_rl_selects_excluded_path(db, manager_with_rl, flow_ctx):
    _open_incident(db, "inc-1", "s1-s3")
    graph = _scenario_a_graph()
    current_path = [1, 3, 6]
    # RL "selects" a nonzero action but its resolved path is the very
    # path being remediated away from -- must fall back, never re-issue
    # the failing path.
    rl_client = FakeRLClient(FakeRLDecision(action=3, chosen_path=current_path))
    manager = manager_with_rl(rl_client)

    chosen_path, results = manager.remediate(
        "inc-1", "s1-s3", flow_ctx, graph, {}, _all_datapaths(), [current_path],
    )

    assert chosen_path is not None
    assert chosen_path != current_path
    row = db.query_one("SELECT * FROM route_changes WHERE incident_id = ?", ("inc-1",))
    assert row["rl_fallback"] == 1
    assert row["rl_fallback_reason"] == "rl_selected_excluded_path"


def test_remediate_falls_back_when_rl_selects_path_not_in_candidates(db, manager_with_rl, flow_ctx):
    _open_incident(db, "inc-1", "s1-s3")
    graph = _scenario_a_graph()
    current_path = [1, 3, 6]
    # A path that doesn't correspond to any real candidate at all --
    # must never be installed as-is.
    rl_client = FakeRLClient(FakeRLDecision(action=1, chosen_path=[1, 2, 3, 4, 5, 6]))
    manager = manager_with_rl(rl_client)

    chosen_path, results = manager.remediate(
        "inc-1", "s1-s3", flow_ctx, graph, {}, _all_datapaths(), [current_path],
    )

    assert chosen_path is not None
    assert chosen_path != [1, 2, 3, 4, 5, 6]
    row = db.query_one("SELECT * FROM route_changes WHERE incident_id = ?", ("inc-1",))
    assert row["rl_fallback"] == 1
    assert row["rl_fallback_reason"] == "rl_selected_invalid_path"


def test_remediate_without_rl_client_records_zero_fallback(db, manager, flow_ctx):
    _open_incident(db, "inc-1", "s1-s3")
    graph = _scenario_a_graph()
    manager.remediate("inc-1", "s1-s3", flow_ctx, graph, {}, _all_datapaths(), [[1, 3, 6]])

    row = db.query_one("SELECT * FROM route_changes WHERE incident_id = ?", ("inc-1",))
    assert row["rl_fallback"] == 0
    assert row["rl_fallback_reason"] is None
    assert row["selector"] == "P1_ML_HEURISTIC"


def test_no_alternate_path_is_recorded_not_silently_dropped(db, manager, flow_ctx):
    _open_incident(db, "inc-1")
    # A graph with only the current path's own edges -- no alternates exist.
    graph = nx.Graph()
    graph.add_nodes_from([1, 3, 6])
    graph.add_edge(1, 3, ports={1: 4, 3: 4})
    graph.add_edge(3, 6, ports={3: 3, 6: 2})

    chosen_path, results = manager.remediate(
        "inc-1", "s1-s3", flow_ctx, graph, {}, _all_datapaths(), excluded_paths=[[1, 3, 6]],
    )

    assert chosen_path is None
    assert results == []

    row = db.query_one("SELECT * FROM route_changes WHERE incident_id = ?", ("inc-1",))
    assert row["changed"] == 0
    assert row["new_path"] is None
    assert "no alternate path" in row["decision_reason"]
