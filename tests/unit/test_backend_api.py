"""Unit tests for backend/api.py's endpoint logic, called directly as
plain functions (no HTTP layer) -- the same pattern
test_ml_rl_service.py established, needed here because starlette's
TestClient requires httpx, which isn't in the approved requirements-ml
stack (see docs/decisions.md). Real end-to-end HTTP behavior against a
running uvicorn process is a live verification, not covered here.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi import HTTPException

from backend import api
from database.db import Database


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    with Database(path):
        pass  # just initialize the schema
    monkeypatch.setattr(api, "_db_path", path)
    return path


def _seed(db_path, table, rows):
    with Database(db_path) as db:
        for row in rows:
            db.insert(table, row)


def test_health():
    result = api.health()
    assert result["status"] == "ok"
    assert "time" in result


def test_get_topology_returns_404_when_none_recorded(db_path):
    with pytest.raises(HTTPException) as exc_info:
        api.get_topology()
    assert exc_info.value.status_code == 404


def test_get_topology_returns_latest_snapshot(db_path):
    _seed(
        db_path, "topology_snapshots",
        [
            {"timestamp": 1.0, "topology_json": json.dumps({"nodes": ["s1"]}), "source": "yaml"},
            {"timestamp": 2.0, "topology_json": json.dumps({"nodes": ["s1", "s2"]}), "source": "lldp"},
        ],
    )
    result = api.get_topology()
    assert result["timestamp"] == 2.0
    assert result["source"] == "lldp"
    assert result["topology"] == {"nodes": ["s1", "s2"]}


def test_get_links(db_path):
    _seed(
        db_path, "links",
        [{"link_id": "s1-s2", "src_switch": "s1", "src_port": 1, "dst_switch": "s2", "dst_port": 1, "state": "HEALTHY"}],
    )
    result = api.get_links()
    assert len(result) == 1
    assert result[0]["link_id"] == "s1-s2"


def test_get_incidents_filters_by_status(db_path):
    _seed(
        db_path, "incidents",
        [
            {"incident_id": "inc-1", "link_id": "s1-s2", "opened_at": 1.0, "status": "OPEN"},
            {"incident_id": "inc-2", "link_id": "s2-s3", "opened_at": 2.0, "status": "RESOLVED"},
        ],
    )
    open_only = api.get_incidents(status="OPEN")
    assert len(open_only) == 1
    assert open_only[0]["incident_id"] == "inc-1"

    all_incidents = api.get_incidents()
    assert len(all_incidents) == 2
    # DESC by opened_at -- most recent first.
    assert all_incidents[0]["incident_id"] == "inc-2"


def test_get_incidents_respects_limit(db_path):
    _seed(
        db_path, "incidents",
        [{"incident_id": f"inc-{i}", "link_id": "s1-s2", "opened_at": float(i), "status": "OPEN"} for i in range(5)],
    )
    result = api.get_incidents(limit=2)
    assert len(result) == 2


def test_get_incident_detail_raises_404_when_missing(db_path):
    with pytest.raises(HTTPException) as exc_info:
        api.get_incident_detail("nope")
    assert exc_info.value.status_code == 404


def test_get_incident_detail_aggregates_related_rows(db_path):
    _seed(db_path, "incidents", [{"incident_id": "inc-1", "link_id": "s1-s2", "opened_at": 1.0, "status": "OPEN"}])
    _seed(
        db_path, "route_changes",
        [{
            "incident_id": "inc-1", "timestamp": 1.0, "flow_key": "h1-h6", "old_path": "s1,s2",
            "new_path": "s1,s3,s2", "decision_reason": "test", "changed": 1, "selector": "P1_ML_HEURISTIC",
            "rl_fallback": 0, "rl_fallback_reason": None,
        }],
    )
    _seed(
        db_path, "verification_results",
        [{
            "incident_id": "inc-1", "timestamp": 2.0, "path": "s1,s3,s2", "classification": "RECOVERED",
            "baseline_json": "{}", "post_json": "{}", "retry_count": 0,
        }],
    )
    _seed(
        db_path, "remediation_events",
        [{"incident_id": "inc-1", "timestamp": 1.5, "event_type": "reroute", "detail_json": "{}"}],
    )

    detail = api.get_incident_detail("inc-1")
    assert detail["incident"]["incident_id"] == "inc-1"
    assert len(detail["route_changes"]) == 1
    assert len(detail["verification_results"]) == 1
    assert len(detail["events"]) == 1
    assert detail["flow_rules"] == []


def test_get_route_changes_filters_by_incident(db_path):
    _seed(
        db_path, "incidents",
        [
            {"incident_id": "inc-1", "link_id": "s1-s2", "opened_at": 0.5, "status": "OPEN"},
            {"incident_id": "inc-2", "link_id": "s2-s5", "opened_at": 0.5, "status": "OPEN"},
        ],
    )
    _seed(
        db_path, "route_changes",
        [
            {
                "incident_id": "inc-1", "timestamp": 1.0, "flow_key": "h1-h6", "old_path": None,
                "new_path": "s1,s2", "decision_reason": "x", "changed": 1, "selector": "P2_ML_RL",
                "rl_fallback": 0, "rl_fallback_reason": None,
            },
            {
                "incident_id": "inc-2", "timestamp": 2.0, "flow_key": "h2-h5", "old_path": None,
                "new_path": "s2,s5", "decision_reason": "y", "changed": 1, "selector": "P1_ML_HEURISTIC",
                "rl_fallback": 1, "rl_fallback_reason": "rl_service_unavailable",
            },
        ],
    )
    result = api.get_route_changes(incident_id="inc-1")
    assert len(result) == 1
    assert result[0]["flow_key"] == "h1-h6"

    all_changes = api.get_route_changes()
    assert len(all_changes) == 2


def test_get_telemetry_filters_by_link_and_since_and_orders_ascending(db_path):
    _seed(
        db_path, "telemetry",
        [
            {
                "timestamp": t, "switch_id": "s1", "port_no": 1, "link_id": "s1-s2",
                "tx_bytes": 0, "rx_bytes": 0, "tx_packets": 0, "rx_packets": 0,
                "rx_dropped": 0, "tx_dropped": 0, "rx_errors": 0, "tx_errors": 0,
                "data_source": "emulated_mininet",
            }
            for t in (1.0, 2.0, 3.0)
        ],
    )
    result = api.get_telemetry("s1-s2", since=1.5)
    assert [r["timestamp"] for r in result] == [2.0, 3.0]  # ascending, > since only


def test_get_predictions_filters_and_orders(db_path):
    _seed(
        db_path, "predictions",
        [
            {"timestamp": t, "link_id": "s1-s2", "probability": 0.1 * t, "threshold": 0.75, "model_version": "x", "horizon_s": 10.0}
            for t in (1.0, 2.0, 3.0)
        ],
    )
    result = api.get_predictions("s1-s2", since=0.0, limit=2)
    # limit applies to the DESC query before re-ascending -- most recent 2, then re-ordered ascending.
    assert [r["timestamp"] for r in result] == [2.0, 3.0]


def test_latest_event_id_is_zero_when_no_events(db_path):
    assert api._latest_event_id() == 0


def test_new_events_since_only_returns_newer_rows(db_path):
    _seed(
        db_path, "remediation_events",
        [{"timestamp": t, "event_type": "x", "detail_json": "{}"} for t in (1.0, 2.0, 3.0)],
    )
    last_id = api._latest_event_id()
    assert last_id == 3
    assert api._new_events_since(last_id) == []
    assert len(api._new_events_since(0)) == 3


async def _collect(gen, n):
    return [await gen.__anext__() for _ in range(n)]


def test_event_stream_only_yields_events_after_it_starts(db_path):
    # A pre-existing event (seeded before the stream connects) must NOT
    # be replayed -- an SSE client only sees what arrives from here on.
    _seed(db_path, "remediation_events", [{"timestamp": time.time(), "event_type": "old", "detail_json": "{}"}])

    async def _run():
        gen = api._event_stream(poll_interval_s=0.01)
        first = await gen.__anext__()  # establishes last_id past the pre-existing event
        assert first == ": keepalive\n\n"

        await asyncio.to_thread(
            _seed, db_path, "remediation_events",
            [{"timestamp": time.time(), "event_type": "reroute", "detail_json": "{}"}],
        )
        second = await gen.__anext__()
        return second

    result = asyncio.run(_run())
    assert result.startswith("data: ")
    assert "reroute" in result
