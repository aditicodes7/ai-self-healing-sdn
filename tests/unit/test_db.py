import sqlite3
import time

import pytest

from common.schema import DataSource, IncidentStatus, LinkHealthState
from database.db import Database

EXPECTED_TABLES = {
    "telemetry",
    "features",
    "predictions",
    "links",
    "topology_snapshots",
    "incidents",
    "route_changes",
    "flow_rules",
    "remediation_events",
    "verification_results",
    "experiment_runs",
}


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "test.db") as database:
        yield database


def test_schema_creates_all_expected_tables(db):
    assert EXPECTED_TABLES.issubset(set(db.table_names()))


def test_wal_mode_enabled(db):
    assert db.journal_mode().lower() == "wal"


def test_init_schema_is_idempotent(tmp_path):
    path = tmp_path / "idempotent.db"
    with Database(path) as first:
        first.insert(
            "links",
            {
                "link_id": "s1-s2",
                "src_switch": "s1",
                "src_port": 1,
                "dst_switch": "s2",
                "dst_port": 1,
                "capacity_bps": 10_000_000,
                "state": LinkHealthState.HEALTHY.value,
                "last_updated": time.time(),
            },
        )
    # Reopening (which re-runs schema.sql) must not wipe existing rows.
    with Database(path) as second:
        rows = second.query("SELECT * FROM links")
        assert len(rows) == 1


def test_indexes_on_timestamp_and_link_id(db):
    telemetry_indexes = db.index_names("telemetry")
    assert any("timestamp" in name for name in telemetry_indexes)

    features_indexes = db.index_names("features")
    assert any("link" in name for name in features_indexes)

    route_changes_indexes = db.index_names("route_changes")
    assert any("incident" in name for name in route_changes_indexes)


def test_insert_and_query_links_row(db):
    rowid = db.insert(
        "links",
        {
            "link_id": "s1-s2",
            "src_switch": "s1",
            "src_port": 1,
            "dst_switch": "s2",
            "dst_port": 2,
            "capacity_bps": 10_000_000,
            "state": LinkHealthState.HEALTHY.value,
            "last_updated": 1700000000.0,
        },
    )
    assert rowid is not None

    row = db.query_one("SELECT * FROM links WHERE link_id = ?", ("s1-s2",))
    assert row["src_switch"] == "s1"
    assert row["state"] == "HEALTHY"


def test_insert_telemetry_requires_data_source_label(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert(
            "telemetry",
            {
                "timestamp": time.time(),
                "switch_id": "s1",
                "port_no": 1,
                "tx_bytes": 100,
                "rx_bytes": 100,
                "tx_packets": 1,
                "rx_packets": 1,
                "rx_dropped": 0,
                "tx_dropped": 0,
                "rx_errors": 0,
                "tx_errors": 0,
                # data_source intentionally omitted: rule 4 requires every
                # dataset row to be honestly labelled, enforced with NOT NULL.
            },
        )


def test_insert_telemetry_with_data_source(db):
    db.insert(
        "telemetry",
        {
            "timestamp": time.time(),
            "switch_id": "s1",
            "port_no": 1,
            "tx_bytes": 100,
            "rx_bytes": 100,
            "tx_packets": 1,
            "rx_packets": 1,
            "rx_dropped": 0,
            "tx_dropped": 0,
            "rx_errors": 0,
            "tx_errors": 0,
            "data_source": DataSource.EMULATED_MININET.value,
        },
    )
    row = db.query_one("SELECT * FROM telemetry WHERE switch_id = ?", ("s1",))
    assert row["data_source"] == "emulated_mininet"


def test_incident_and_route_change_foreign_key(db):
    db.insert(
        "incidents",
        {
            "incident_id": "inc-1",
            "link_id": "s1-s2",
            "opened_at": time.time(),
            "status": IncidentStatus.OPEN.value,
        },
    )
    db.insert(
        "route_changes",
        {
            "incident_id": "inc-1",
            "timestamp": time.time(),
            "flow_key": "h1-h6",
            "old_path": "s1,s3,s6",
            "new_path": "s1,s4,s6",
            "decision_reason": "persistence confirmed 3 of 4 windows",
            "changed": 1,
            "selector": "P2_ML_RL",
        },
    )
    rows = db.query(
        "SELECT * FROM route_changes WHERE incident_id = ?", ("inc-1",)
    )
    assert len(rows) == 1
    assert rows[0]["changed"] == 1


def test_generic_insert_rejects_unknown_column(db):
    with pytest.raises(sqlite3.OperationalError):
        db.insert("links", {"not_a_real_column": 1})
