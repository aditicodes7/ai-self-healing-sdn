"""Unit tests for the assembled closed loop (controller/healing_loop.py).

Everything the loop needs is injected, so the whole
prediction -> persistence -> stability -> remediate -> verify chain runs
here synchronously with no ryu, no Mininet, and no real sleeping.
"""
from __future__ import annotations

import networkx as nx
import pytest

from common.schema import IncidentStatus, VerificationClassification
from controller.healing_loop import HealingLoop
from controller.path_computer import LinkMetrics
from controller.persistence_tracker import PersistenceTracker
from controller.remediation_manager import FlowContext
from controller.stability_manager import StabilityManager
from controller.verifier import Baseline, VerificationOutcome
from database.db import Database

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

THRESHOLD = 0.75
BAND = (0.45, 0.60)


def _graph() -> nx.Graph:
    g = nx.Graph()
    g.add_nodes_from(range(1, 7))
    for u, v, data in SCENARIO_A_EDGES:
        g.add_edge(u, v, **data)
    return g


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "loop.db") as database:
        yield database


@pytest.fixture
def flow_ctx():
    return FlowContext(
        flow_key="h1-h6", src_dpid=1, dst_dpid=6,
        src_host_port=1, dst_host_port=1,
        ipv4_src="10.0.0.1", ipv4_dst="10.0.0.6",
    )


class FakeRemediation:
    """Stands in for RemediationManager: records what it was asked to do
    and returns a path that isn't excluded."""

    def __init__(self, graph, always_fail=False):
        self.graph = graph
        self.calls = []
        self.always_fail = always_fail

    def remediate(self, incident_id, link_id, flow_ctx, graph, link_metrics,
                  datapaths, excluded_paths, **kwargs):
        self.calls.append({"incident_id": incident_id, "link_id": link_id, "excluded": list(excluded_paths)})
        if self.always_fail:
            return None, []
        from controller.path_computer import generate_candidate_paths

        for path in generate_candidate_paths(graph, flow_ctx.src_dpid, flow_ctx.dst_dpid, 4):
            if path not in excluded_paths:
                return path, []
        return None, []


class FakeVerifier:
    """Stands in for Verifier, so the loop's own orchestration is what's
    under test here rather than verifier.py's retry maths (which has its
    own tests)."""

    def __init__(self, classification=VerificationClassification.SUCCESS):
        self.classification = classification
        self.calls = []

    def verify_and_retry(self, incident_id, baseline, remediate_fn, measure_fn, sleep_fn):
        self.calls.append({"incident_id": incident_id, "baseline": baseline})
        remediate_fn([])  # the loop must actually install something
        return VerificationOutcome(
            classification=self.classification,
            post_latency_ms=5.0,
            post_loss_rate=0.0,
            reason=f"fake {self.classification.value}",
        )


def _make_loop(db, flow_ctx, *, link_metrics=None, remediation=None, verifier=None,
               cooldown_s=0.0, route_hold_s=0.0, min_improvement=0.0):
    graph = _graph()
    metrics = link_metrics if link_metrics is not None else {}
    return HealingLoop(
        db=db,
        persistence=PersistenceTracker(window=4, consecutive=3, uncertainty_band=BAND, threshold=THRESHOLD),
        stability=StabilityManager(cooldown_s=cooldown_s, route_hold_s=route_hold_s, min_improvement=min_improvement),
        remediation=remediation if remediation is not None else FakeRemediation(graph),
        verifier=verifier if verifier is not None else FakeVerifier(),
        flow_ctx=flow_ctx,
        graph_provider=lambda: graph,
        datapaths_provider=lambda: {d: object() for d in range(1, 7)},
        link_metrics_provider=lambda: metrics,
        k_shortest=4,
        weight_utilization=1.0,
        weight_latency=1.0,
        weight_loss=2.0,
        weight_hop_count=0.25,
        sleep_fn=lambda s: None,
    )


def _push(loop, link_id, probability, times=4):
    for _ in range(times):
        loop.on_prediction(link_id, probability)


# -- initial path -------------------------------------------------------


def test_install_initial_path_sets_active_path(db, flow_ctx):
    loop = _make_loop(db, flow_ctx)
    path = loop.install_initial_path()
    assert path is not None
    assert loop.active_path == path
    assert path[0] == 1 and path[-1] == 6


def test_install_initial_path_records_incident_and_event(db, flow_ctx):
    loop = _make_loop(db, flow_ctx)
    loop.install_initial_path()
    row = db.query_one("SELECT * FROM incidents ORDER BY opened_at DESC LIMIT 1")
    assert row["status"] == IncidentStatus.SUCCESS.value
    event = db.query_one("SELECT * FROM remediation_events ORDER BY id DESC LIMIT 1")
    assert event["event_type"] == "initial_path_installed"


def test_install_initial_path_returns_none_when_no_route(db, flow_ctx):
    disconnected = nx.Graph()
    disconnected.add_nodes_from([1, 6])
    loop = _make_loop(db, flow_ctx)
    loop.graph_provider = lambda: disconnected
    assert loop.install_initial_path() is None


# -- the decision gates -------------------------------------------------


def test_low_probability_never_triggers_remediation(db, flow_ctx):
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.install_initial_path()
    before = len(remediation.calls)

    _push(loop, loop._active_path_link_ids()[0], 0.10)

    assert len(remediation.calls) == before  # only the initial install


def test_uncertain_probability_abstains(db, flow_ctx):
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.install_initial_path()
    before = len(remediation.calls)

    _push(loop, loop._active_path_link_ids()[0], 0.50)  # inside [0.45, 0.60]

    assert len(remediation.calls) == before


def test_single_high_reading_is_not_enough(db, flow_ctx):
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.install_initial_path()
    before = len(remediation.calls)

    loop.on_prediction(loop._active_path_link_ids()[0], 0.95)  # one reading only

    assert len(remediation.calls) == before


def test_persistent_high_probability_on_active_path_triggers_reroute(db, flow_ctx):
    remediation = FakeRemediation(_graph())
    verifier = FakeVerifier()
    loop = _make_loop(db, flow_ctx, remediation=remediation, verifier=verifier)
    loop.install_initial_path()
    original = list(loop.active_path)

    _push(loop, loop._active_path_link_ids()[0], 0.95)

    assert verifier.calls, "verification should have run"
    assert loop.active_path != original, "the loop should have moved off the failing path"


def test_degraded_link_not_on_active_path_is_ignored(db, flow_ctx):
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.install_initial_path()
    before = len(remediation.calls)

    on_path = set(loop._active_path_link_ids())
    off_path = next(
        lid for lid in ("s1-s2", "s2-s3", "s3-s6", "s1-s4", "s4-s5", "s5-s6", "s1-s3", "s2-s5")
        if lid not in on_path
    )
    _push(loop, off_path, 0.99)

    assert len(remediation.calls) == before


def test_no_action_before_initial_path_is_known(db, flow_ctx):
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    # deliberately skip install_initial_path()
    _push(loop, "s1-s3", 0.99)
    assert remediation.calls == []


def test_cooldown_blocks_a_second_reroute(db, flow_ctx):
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation, cooldown_s=9999.0)
    loop.install_initial_path()

    _push(loop, loop._active_path_link_ids()[0], 0.95)
    calls_after_first = len(remediation.calls)

    _push(loop, loop._active_path_link_ids()[0], 0.95)
    assert len(remediation.calls) == calls_after_first, "cooldown should have blocked the second reroute"


# -- outcomes are recorded honestly --------------------------------------


def test_successful_verification_closes_incident_as_success(db, flow_ctx):
    loop = _make_loop(db, flow_ctx, verifier=FakeVerifier(VerificationClassification.SUCCESS))
    loop.install_initial_path()
    _push(loop, loop._active_path_link_ids()[0], 0.95)

    row = db.query_one(
        "SELECT * FROM incidents WHERE incident_id LIKE 'inc-%' ORDER BY opened_at DESC LIMIT 1"
    )
    assert row["status"] == IncidentStatus.SUCCESS.value
    assert row["closed_at"] is not None


def test_failed_verification_closes_incident_as_failed(db, flow_ctx):
    loop = _make_loop(db, flow_ctx, verifier=FakeVerifier(VerificationClassification.FAILED))
    loop.install_initial_path()
    _push(loop, loop._active_path_link_ids()[0], 0.95)

    row = db.query_one(
        "SELECT * FROM incidents WHERE incident_id LIKE 'inc-%' ORDER BY opened_at DESC LIMIT 1"
    )
    assert row["status"] == IncidentStatus.FAILED.value


def test_events_are_emitted_for_the_dashboard(db, flow_ctx):
    loop = _make_loop(db, flow_ctx)
    loop.install_initial_path()
    _push(loop, loop._active_path_link_ids()[0], 0.95)

    types = [r["event_type"] for r in db.query("SELECT event_type FROM remediation_events ORDER BY id")]
    assert "degradation_predicted" in types
    assert "reroute_installed" in types
    assert any(t.startswith("verification_") for t in types)


def test_exception_during_remediation_closes_incident_as_unresolved(db, flow_ctx):
    class ExplodingVerifier:
        def verify_and_retry(self, **kwargs):
            raise RuntimeError("boom")

    loop = _make_loop(db, flow_ctx, verifier=ExplodingVerifier())
    loop.install_initial_path()
    _push(loop, loop._active_path_link_ids()[0], 0.95)

    row = db.query_one(
        "SELECT * FROM incidents WHERE incident_id LIKE 'inc-%' ORDER BY opened_at DESC LIMIT 1"
    )
    assert row["status"] == IncidentStatus.UNRESOLVED.value
    # and the loop must not be wedged for the next prediction
    assert loop._remediation_in_flight is False


def test_concurrent_predictions_do_not_open_duplicate_incidents(db, flow_ctx):
    """While one remediation is in flight, further predictions must not
    start a second one -- predictions keep arriving every 2s while a
    verification is still settling."""

    class ReentrantVerifier:
        def __init__(self):
            self.loop = None
            self.depth = 0
            self.max_depth = 0

        def verify_and_retry(self, incident_id, baseline, remediate_fn, measure_fn, sleep_fn):
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
            try:
                # Predictions keep arriving while this verification is
                # still settling; none of them may start a nested
                # remediation.
                _push(self.loop, self.loop._active_path_link_ids()[0], 0.99)
                remediate_fn([])
                return VerificationOutcome(VerificationClassification.SUCCESS, 1.0, 0.0, "ok")
            finally:
                self.depth -= 1

    verifier = ReentrantVerifier()
    loop = _make_loop(db, flow_ctx, verifier=verifier)
    verifier.loop = loop
    loop.install_initial_path()

    _push(loop, loop._active_path_link_ids()[0], 0.95)

    assert verifier.max_depth == 1, "a remediation started while another was already in flight"


# -- regressions for bugs found when the loop was first assembled --------


def test_reroute_never_reselects_the_failing_path(db, flow_ctx):
    """Regression: the verifier passes an empty exclusion list on its
    first attempt, so without the loop seeding the failing path itself,
    remediation could 'fix' an incident by re-choosing the very path that
    was failing."""
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.install_initial_path()
    failing = list(loop.active_path)

    _push(loop, loop._active_path_link_ids()[0], 0.95)

    reroute_calls = [c for c in remediation.calls if c["link_id"]]
    assert reroute_calls, "a reroute should have been attempted"
    for call in reroute_calls:
        assert failing in call["excluded"], "the failing path must be excluded from every attempt"
    assert loop.active_path != failing


def test_reroute_excludes_every_path_crossing_the_degrading_link(db, flow_ctx):
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.install_initial_path()
    bad_link = loop._active_path_link_ids()[0]

    _push(loop, bad_link, 0.95)

    assert bad_link not in loop._path_link_ids(loop.active_path), (
        "the loop settled on a path that still crosses the degrading link"
    )


def test_prediction_is_folded_into_path_cost(db, flow_ctx):
    """Regression: costing a degrading link by its still-healthy current
    counters made every alternative look worse, so the min-improvement
    gate blocked all reroutes and the ML pipeline had no effect on
    routing."""
    loop = _make_loop(db, flow_ctx)
    metrics = {"s1-s3": LinkMetrics(utilization=0.1, loss_rate=0.0, latency_ms=1.0)}

    adjusted = loop._metrics_with_prediction(metrics, "s1-s3", 0.95)

    assert adjusted["s1-s3"].utilization == 0.95
    assert adjusted["s1-s3"].loss_rate == 0.95
    assert adjusted["s1-s3"].latency_ms == 1.0  # measured value preserved
    # other links untouched
    assert "s3-s6" not in adjusted


def test_prediction_adjustment_never_lowers_a_measured_value(db, flow_ctx):
    # Measured reality is already worse than the model's confidence:
    # the adjustment must leave both values alone, not soften them.
    loop = _make_loop(db, flow_ctx)
    metrics = {"s1-s3": LinkMetrics(utilization=0.99, loss_rate=0.80)}
    adjusted = loop._metrics_with_prediction(metrics, "s1-s3", 0.60)
    assert adjusted["s1-s3"].utilization == 0.99
    assert adjusted["s1-s3"].loss_rate == 0.80


def test_no_remediation_when_every_alternative_crosses_the_bad_link(db, flow_ctx):
    """A chain topology where s1->s6 has exactly one route: if that
    route's link degrades there is nothing to reroute to, and the loop
    must say so rather than thrash."""
    chain = nx.Graph()
    chain.add_edge(1, 3, ports={1: 4, 3: 4})
    chain.add_edge(3, 6, ports={3: 3, 6: 2})
    remediation = FakeRemediation(chain)
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.graph_provider = lambda: chain
    loop.remediation = remediation
    loop.install_initial_path()
    before = len(remediation.calls)

    _push(loop, "s1-s3", 0.99)

    assert len(remediation.calls) == before, "should not attempt an impossible reroute"


# -- systems under test (Phase 22) ---------------------------------------


def test_b1_never_reroutes(db, flow_ctx):
    """B1 is the do-nothing control: it installs a path once and lets the
    fault's full cost be measured."""
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.system = "B1_SHORTEST_PATH"
    loop.install_initial_path()
    before = len(remediation.calls)

    _push(loop, loop._active_path_link_ids()[0], 0.99, times=10)

    assert len(remediation.calls) == before, "B1 must never react to a prediction"


def test_b2_ignores_prediction_and_reacts_to_current_metrics(db, flow_ctx):
    """B2 must reroute on measured conditions alone -- a sky-high
    prediction with healthy links must NOT move it."""
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.system = "B2_RULE_BASED"
    loop.install_initial_path()
    before = len(remediation.calls)

    # Certain prediction, but every link is measured healthy.
    _push(loop, loop._active_path_link_ids()[0], 0.99, times=6)
    assert len(remediation.calls) == before, "B2 must ignore predictions"


def test_b2_reroutes_when_measured_utilization_exceeds_threshold(db, flow_ctx):
    graph = _graph()
    remediation = FakeRemediation(graph)
    metrics = {"s1-s3": LinkMetrics(utilization=0.95, loss_rate=0.0, latency_ms=5.0)}
    loop = _make_loop(db, flow_ctx, remediation=remediation, link_metrics=metrics)
    loop.system = "B2_RULE_BASED"
    loop.b2_utilization_threshold = 0.80
    loop.install_initial_path()
    assert "s1-s3" in loop._active_path_link_ids()
    before = len(remediation.calls)

    # Probability is irrelevant to B2; the measured 0.95 utilization is not.
    loop.on_prediction("s1-s3", 0.0)

    assert len(remediation.calls) > before, "B2 should react to measured congestion"


def test_p1_and_p2_differ_only_by_rl_client(db, flow_ctx):
    """P1 is P2 without an RL client, so the healing loop itself must not
    branch on the distinction -- otherwise E6's comparison would be
    measuring more than path selection."""
    loop = _make_loop(db, flow_ctx)
    for system in ("P1_ML_HEURISTIC", "P2_ML_RL"):
        loop.system = system
        loop.install_initial_path()
        assert loop.active_path is not None


# -- measurement ---------------------------------------------------------


def test_measure_active_path_sums_latency_and_compounds_loss(db, flow_ctx):
    metrics = {
        "s1-s3": LinkMetrics(latency_ms=2.0, loss_rate=0.1),
        "s3-s6": LinkMetrics(latency_ms=3.0, loss_rate=0.2),
    }
    loop = _make_loop(db, flow_ctx, link_metrics=metrics)
    loop.active_path = [1, 3, 6]

    latency, loss = loop._measure_active_path()
    assert latency == 5.0
    # 1 - (0.9 * 0.8) = 0.28
    assert loss == pytest.approx(0.28)


def test_measure_active_path_returns_none_when_nothing_measured(db, flow_ctx):
    loop = _make_loop(db, flow_ctx, link_metrics={})
    loop.active_path = [1, 3, 6]
    latency, loss = loop._measure_active_path()
    assert latency is None and loss is None


def test_measure_active_path_with_no_path_returns_none(db, flow_ctx):
    loop = _make_loop(db, flow_ctx)
    assert loop._measure_active_path() == (None, None)


def test_stale_link_states_expire_back_to_healthy(db, flow_ctx):
    """A remediated link was left AT_RISK forever, so after a long run
    the dashboard listed six links as 'starting to look risky' when they
    were simply ones rerouted around at some point. A health state with
    no expiry is a running total of everything that ever went wrong."""
    import time as _t

    loop = _make_loop(db, flow_ctx)
    db.insert("links", {
        "link_id": "s1-s3", "src_switch": "s1", "src_port": 4,
        "dst_switch": "s3", "dst_port": 4, "state": "AT_RISK",
        "last_updated": _t.time() - 600,
    })
    db.insert("links", {
        "link_id": "s1-s2", "src_switch": "s1", "src_port": 2,
        "dst_switch": "s2", "dst_port": 2, "state": "DEGRADED",
        "last_updated": _t.time(),  # fresh: an incident may be live
    })

    loop.clear_stale_link_states(max_age_s=60)

    states = {r["link_id"]: r["state"] for r in db.query("SELECT link_id, state FROM links")}
    assert states["s1-s3"] == "HEALTHY", "a stale state should expire"
    assert states["s1-s2"] == "DEGRADED", "a fresh state must not be cleared mid-incident"


def test_expiry_leaves_healthy_links_alone(db, flow_ctx):
    import time as _t

    loop = _make_loop(db, flow_ctx)
    db.insert("links", {
        "link_id": "s3-s6", "src_switch": "s3", "src_port": 3,
        "dst_switch": "s6", "dst_port": 2, "state": "HEALTHY",
        "last_updated": _t.time() - 9999,
    })
    loop.clear_stale_link_states(max_age_s=60)
    row = db.query_one("SELECT state FROM links WHERE link_id = 's3-s6'")
    assert row["state"] == "HEALTHY"


# -- hard link failure, via topology discovery ---------------------------


def test_link_down_on_active_path_triggers_remediation(db, flow_ctx):
    """The ML predictor cannot see a hard failure: measured live, a cut
    link kept scoring 0.034 -- LOWER than before the cut -- because a
    dead link carries no traffic and returns no latency, which looks
    exactly like an idle healthy one. LLDP's removal event is the signal
    instead."""
    remediation = FakeRemediation(_graph())
    verifier = FakeVerifier()
    loop = _make_loop(db, flow_ctx, remediation=remediation, verifier=verifier)
    loop.install_initial_path()
    original = list(loop.active_path)

    loop.on_link_down(loop._active_path_link_ids()[0])

    assert verifier.calls, "a link-down on the active path must remediate"
    assert loop.active_path != original


def test_link_down_bypasses_persistence(db, flow_ctx):
    """Persistence filters uncertain PREDICTIONS. A disappearance is an
    observed fact, so requiring 3-of-4 corroboration would only add
    latency to something already certain."""
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.install_initial_path()
    before = len(remediation.calls)

    # A single event, with no prior readings recorded at all.
    loop.on_link_down(loop._active_path_link_ids()[0])

    assert len(remediation.calls) > before


def test_link_down_off_active_path_is_ignored(db, flow_ctx):
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.install_initial_path()
    before = len(remediation.calls)

    on_path = set(loop._active_path_link_ids())
    off_path = next(
        lid for lid in ("s1-s2", "s2-s3", "s3-s6", "s1-s4", "s4-s5", "s5-s6", "s1-s3", "s2-s5")
        if lid not in on_path
    )
    loop.on_link_down(off_path)

    assert len(remediation.calls) == before


def test_b1_ignores_link_down(db, flow_ctx):
    """B1 is the do-nothing control and must stay inert, or it stops
    being a control."""
    remediation = FakeRemediation(_graph())
    loop = _make_loop(db, flow_ctx, remediation=remediation)
    loop.system = "B1_SHORTEST_PATH"
    loop.install_initial_path()
    before = len(remediation.calls)

    loop.on_link_down(loop._active_path_link_ids()[0])

    assert len(remediation.calls) == before


def test_link_down_marks_the_link_failed(db, flow_ctx):
    loop = _make_loop(db, flow_ctx)
    loop.install_initial_path()
    link = loop._active_path_link_ids()[0]
    db.insert("links", {
        "link_id": link, "src_switch": "s1", "src_port": 1,
        "dst_switch": "s3", "dst_port": 1, "state": "HEALTHY",
        "last_updated": 0.0,
    })

    loop.on_link_down(link)

    row = db.query_one("SELECT state FROM links WHERE link_id = ?", (link,))
    assert row["state"] == "FAILED"


def test_link_down_does_not_start_a_second_remediation(db, flow_ctx):
    loop = _make_loop(db, flow_ctx)
    loop.install_initial_path()
    loop._remediation_in_flight = True
    remediation_calls_before = len(loop.remediation.calls)

    loop.on_link_down(loop._active_path_link_ids()[0])

    assert len(loop.remediation.calls) == remediation_calls_before
