import json

import pytest

from controller.flow_manager import FlowManager, FlowMatch, PathHop, incident_cookie
from common.schema import FlowRuleResult
from database.db import Database


class _MockOfproto:
    OFPIT_APPLY_ACTIONS = 4


class _MockParser:
    def OFPMatch(self, **kwargs):
        return {"kind": "match", **kwargs}

    def OFPActionOutput(self, port):
        return {"kind": "action_output", "port": port}

    def OFPInstructionActions(self, instr_type, actions):
        return {"kind": "instruction", "actions": actions}

    def OFPFlowMod(self, **kwargs):
        return {"kind": "flowmod", **kwargs}

    def OFPBarrierRequest(self, datapath):
        return {"kind": "barrier"}


class MockDatapath:
    """Stands in for a real Ryu datapath: records every message handed to
    send_msg() so tests can assert the exact FlowMod fields emitted,
    matching the project's own stated testing approach ("mocked datapath
    asserting the exact FlowMods emitted")."""

    def __init__(self, dpid: int, raise_on_send: bool = False):
        self.id = dpid
        self.ofproto = _MockOfproto()
        self.ofproto_parser = _MockParser()
        self.sent_messages = []
        self._raise_on_send = raise_on_send

    def send_msg(self, msg):
        if self._raise_on_send:
            raise ConnectionError("simulated datapath disconnect")
        self.sent_messages.append(msg)
        return None


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "test.db") as database:
        yield database


def _open_incident(db, incident_id: str, link_id: str = "s1-s2") -> None:
    # flow_rules.incident_id is a real foreign key against incidents --
    # production code opens the incident before remediating; tests must
    # do the same rather than relying on a constraint that doesn't exist.
    import time

    db.insert(
        "incidents",
        {"incident_id": incident_id, "link_id": link_id, "opened_at": time.time(), "status": "OPEN"},
    )


@pytest.fixture
def manager(db):
    return FlowManager(db, priority=200, hard_timeout_s=300, idle_timeout_s=0)


def test_install_path_sends_correct_flowmod_fields(manager, db):
    _open_incident(db, "inc-1")
    dp1 = MockDatapath(1)
    dp2 = MockDatapath(2)
    hops = [
        PathHop(dpid=1, in_port=1, out_port=2),
        PathHop(dpid=2, in_port=2, out_port=1),
    ]
    match = FlowMatch(ipv4_src="10.0.0.1", ipv4_dst="10.0.0.6")

    results = manager.install_path("inc-1", hops, {1: dp1, 2: dp2}, match)

    assert all(r.result == FlowRuleResult.INSTALLED for r in results)
    assert len(results) == 2

    for dp, hop in ((dp1, hops[0]), (dp2, hops[1])):
        flowmod_msgs = [m for m in dp.sent_messages if m["kind"] == "flowmod"]
        assert len(flowmod_msgs) == 1
        mod = flowmod_msgs[0]
        assert mod["priority"] == 200
        assert mod["hard_timeout"] == 300
        assert mod["idle_timeout"] == 0
        assert mod["cookie"] == incident_cookie("inc-1")

        m = mod["match"]
        assert m["in_port"] == hop.in_port
        assert m["eth_type"] == 0x0800
        assert m["ipv4_src"] == "10.0.0.1"
        assert m["ipv4_dst"] == "10.0.0.6"

        actions = mod["instructions"][0]["actions"]
        assert actions == [{"kind": "action_output", "port": hop.out_port}]

        # Barrier sent after the FlowMod.
        barrier_msgs = [m for m in dp.sent_messages if m["kind"] == "barrier"]
        assert len(barrier_msgs) == 1


def test_install_path_logs_every_hop_to_db(manager, db):
    _open_incident(db, "inc-42")
    dp1 = MockDatapath(1)
    hops = [PathHop(dpid=1, in_port=1, out_port=2)]
    match = FlowMatch(ipv4_src="10.0.0.1", ipv4_dst="10.0.0.6")

    manager.install_path("inc-42", hops, {1: dp1}, match)

    rows = db.query("SELECT * FROM flow_rules WHERE incident_id = ?", ("inc-42",))
    assert len(rows) == 1
    row = rows[0]
    assert row["switch_id"] == "1"
    assert row["result"] == "INSTALLED"
    assert row["cookie"] == incident_cookie("inc-42")
    match_json = json.loads(row["match_json"])
    assert match_json["ipv4_dst"] == "10.0.0.6"


def test_missing_datapath_is_logged_as_error_not_a_crash(manager, db):
    _open_incident(db, "inc-1")
    hops = [PathHop(dpid=99, in_port=1, out_port=2)]
    match = FlowMatch(ipv4_src="10.0.0.1", ipv4_dst="10.0.0.6")

    results = manager.install_path("inc-1", hops, {}, match)  # dpid 99 not connected

    assert len(results) == 1
    assert results[0].result == FlowRuleResult.ERROR
    assert "not connected" in results[0].error_msg

    row = db.query_one("SELECT * FROM flow_rules WHERE incident_id = ?", ("inc-1",))
    assert row["result"] == "ERROR"


def test_send_msg_exception_is_caught_and_logged_as_error(manager, db):
    _open_incident(db, "inc-1")
    dp1 = MockDatapath(1, raise_on_send=True)
    hops = [PathHop(dpid=1, in_port=1, out_port=2)]
    match = FlowMatch(ipv4_src="10.0.0.1", ipv4_dst="10.0.0.6")

    results = manager.install_path("inc-1", hops, {1: dp1}, match)

    assert results[0].result == FlowRuleResult.ERROR
    assert "disconnect" in results[0].error_msg


def test_incident_cookie_is_deterministic_and_fits_64_bits():
    c1 = incident_cookie("inc-1")
    c2 = incident_cookie("inc-1")
    c3 = incident_cookie("inc-2")
    assert c1 == c2
    assert c1 != c3
    assert 0 <= c1 < 2**64


def test_incident_cookie_always_fits_sqlite_signed_int64():
    # Regression test: SQLite's INTEGER column is signed 64-bit
    # (max 2**63-1), but a naive unsigned-64-bit hash overflows it ~50%
    # of the time. Confirmed the hard way on a live VM run (2026-09-03):
    # this crashed mid-install_path(), leaving a multi-hop path with only
    # its first switch actually flowed. Check many incident_ids, not just
    # one, since the bug is probabilistic per input.
    sqlite_int64_max = 2**63 - 1
    for i in range(2000):
        cookie = incident_cookie(f"incident-{i}")
        assert 0 <= cookie <= sqlite_int64_max, f"incident-{i} produced an out-of-range cookie {cookie}"


def test_one_hop_logging_failure_does_not_abort_remaining_hops(manager, db, monkeypatch):
    # Regression test for the exact failure this bug caused: hop 1's
    # _log() raising must not prevent hops 2+ from being attempted.
    _open_incident(db, "inc-1")
    dp1 = MockDatapath(1)
    dp2 = MockDatapath(2)
    hops = [
        PathHop(dpid=1, in_port=1, out_port=2),
        PathHop(dpid=2, in_port=2, out_port=1),
    ]
    match = FlowMatch(ipv4_src="10.0.0.1", ipv4_dst="10.0.0.6")

    real_log = manager._log
    call_count = {"n": 0}

    def flaky_log(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OverflowError("simulated SQLite overflow on first hop")
        return real_log(*args, **kwargs)

    monkeypatch.setattr(manager, "_log", flaky_log)

    results = manager.install_path("inc-1", hops, {1: dp1, 2: dp2}, match)

    assert len(results) == 2  # both hops attempted, not just the first
    assert results[0].result == FlowRuleResult.ERROR  # hop 1's logging failed
    assert results[1].result == FlowRuleResult.INSTALLED  # hop 2 still succeeded
    # The actual FlowMod for hop 2 (and its barrier) still reached the switch
    # despite hop 1's logging failure.
    assert any(m["kind"] == "flowmod" for m in dp2.sent_messages)


def test_partial_path_failure_still_attempts_remaining_hops(manager, db):
    _open_incident(db, "inc-1")
    dp1 = MockDatapath(1, raise_on_send=True)
    dp2 = MockDatapath(2)
    hops = [
        PathHop(dpid=1, in_port=1, out_port=2),
        PathHop(dpid=2, in_port=2, out_port=1),
    ]
    match = FlowMatch(ipv4_src="10.0.0.1", ipv4_dst="10.0.0.6")

    results = manager.install_path("inc-1", hops, {1: dp1, 2: dp2}, match)

    assert results[0].result == FlowRuleResult.ERROR
    assert results[1].result == FlowRuleResult.INSTALLED
