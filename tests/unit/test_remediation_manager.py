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
                      global_mean_utilization, global_max_utilization, allow_hold=True):
        self.calls.append(
            {
                "src_dpid": src_dpid, "dst_dpid": dst_dpid, "candidates": candidates,
                "current_path": current_path, "degraded_link_id": degraded_link_id,
                "allow_hold": allow_hold,
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


class RecordingRLClient(FakeRLClient):
    """Captures the candidate list the policy was actually offered."""

    def select_route(self, src_dpid, dst_dpid, candidates, *args, **kwargs):
        self.offered = list(candidates)
        return super().select_route(src_dpid, dst_dpid, candidates, *args, **kwargs)


def test_rl_is_only_offered_paths_it_may_actually_choose(db, manager_with_rl, flow_ctx):
    """Observed live on a link_failure: the policy was handed the full
    k-shortest list, chose a path crossing the dead link, and the safety
    gate threw the answer away (rl_selected_excluded_path). Asking for a
    decision and then discarding it wastes the policy."""
    _open_incident(db, "inc-1", "s1-s3")
    graph = _scenario_a_graph()
    failing = [1, 3, 6]
    rl_client = RecordingRLClient(FakeRLDecision(action=1, chosen_path=[1, 2, 3, 6]))
    manager = manager_with_rl(rl_client)

    manager.remediate("inc-1", "s1-s3", flow_ctx, graph, {}, _all_datapaths(), [failing])

    assert rl_client.offered, "the policy should have been consulted"
    for path in rl_client.offered:
        assert path != failing, "the failing path was offered to the policy"
        assert "s1-s3" not in manager._path_link_ids(path), (
            f"{path} crosses the degrading link and should not have been offered"
        )


def test_rl_used_when_it_picks_from_the_offered_set(db, manager_with_rl, flow_ctx):
    _open_incident(db, "inc-1", "s1-s3")
    graph = _scenario_a_graph()
    rl_client = RecordingRLClient(FakeRLDecision(action=1, chosen_path=None))
    manager = manager_with_rl(rl_client)

    # Answer with whatever the manager actually offered first.
    def pick_first(src, dst, candidates, *a, **k):
        rl_client.offered = list(candidates)
        return FakeRLDecision(action=1, chosen_path=candidates[0])

    rl_client.select_route = pick_first
    chosen, _ = manager.remediate(
        "inc-1", "s1-s3", flow_ctx, graph, {}, _all_datapaths(), [[1, 3, 6]]
    )

    row = db.query_one("SELECT * FROM route_changes WHERE incident_id = ? AND changed = 1", ("inc-1",))
    assert row["selector"] == "P2_ML_RL"
    assert row["rl_fallback"] == 0
    assert "s1-s3" not in manager._path_link_ids(chosen)


def test_no_viable_candidate_for_rl_is_reported_distinctly(db, manager_with_rl, flow_ctx):
    """A chain topology where every route crosses the bad link: the
    policy cannot be offered anything, and that is a different situation
    from it choosing badly."""
    chain = nx.Graph()
    chain.add_edge(1, 3, ports={1: 4, 3: 4})
    chain.add_edge(3, 6, ports={3: 3, 6: 2})
    _open_incident(db, "inc-1", "s1-s3")
    rl_client = RecordingRLClient(FakeRLDecision(action=1, chosen_path=[1, 3, 6]))
    manager = manager_with_rl(rl_client)

    manager.remediate("inc-1", "s1-s3", flow_ctx, chain, {}, _all_datapaths(), [[1, 3, 6]])

    row = db.query_one("SELECT * FROM route_changes WHERE incident_id = ?", ("inc-1",))
    assert row["rl_fallback_reason"] == "no_viable_candidate_for_rl"


def test_rl_is_not_allowed_to_answer_hold_during_remediation(db, manager_with_rl, flow_ctx):
    """The policy is consulted only after the healing loop has opened an
    incident and persistence is satisfied, so "do not reroute" there
    contradicts the decision that caused the call.

    Measured in E6 before this was fixed: the policy answered action 0 in
    7 of 11 selections, the manager discarded the answer
    (`rl_selected_no_reroute`) and used the weighted-cost heuristic
    instead. P1 and P2 are supposed to differ ONLY in path selection, so
    that quietly turned the experiment into the heuristic compared against
    itself, with the RL credited for decisions it did not make.
    """
    _open_incident(db, "inc-1", "s1-s3")
    rl_client = RecordingRLClient(FakeRLDecision(action=1, chosen_path=[1, 2, 3, 6]))
    manager = manager_with_rl(rl_client)

    manager.remediate("inc-1", "s1-s3", flow_ctx, _scenario_a_graph(), {},
                      _all_datapaths(), [[1, 3, 6]])

    assert rl_client.calls, "the policy should have been consulted"
    assert rl_client.calls[-1]["allow_hold"] is False, (
        "remediation must mask out the hold action; letting the policy decline "
        "hands the decision to the heuristic and misattributes it to the RL"
    )
