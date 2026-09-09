#!/usr/bin/env python3
"""FastAPI backend serving the dashboard (Phase 20): REST endpoints over
the shared SQLite database (database/schema.sql) plus a Server-Sent
Events stream for live updates as the controller (running in the VM)
writes new remediation_events rows.

No message queue / pubsub -- not in the approved stack (stdlib sqlite3,
no ORM, no broker). The SSE stream is a simple polling loop over
remediation_events.id, matching that constraint honestly rather than
faking real push semantics with something heavier.

Endpoint functions take plain typed defaults (not FastAPI's Query(...)
wrapper) specifically so they stay callable directly as plain Python
functions in tests -- the same "no HTTP layer" pattern
test_ml_rl_service.py already established, needed here because
starlette's TestClient requires httpx, which isn't in the approved
requirements-ml.txt stack (see docs/decisions.md).

Runs under venv-ml (FastAPI/uvicorn already required there for
service/ml_rl_service.py). Run with:
venv-ml/bin/uvicorn backend.api:app --host <cfg> --port <cfg>
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.config_loader import load_config  # noqa: E402
from common.logging_setup import configure_logging  # noqa: E402
from database.db import Database  # noqa: E402

logger = configure_logging("backend.api")

app = FastAPI(title="ai-self-healing-sdn backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_config = load_config()
_db_path = REPO_ROOT / _config.database.path


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _db() -> Database:
    return Database(_db_path, wal=_config.database.wal)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "time": time.time()}


@app.get("/api/topology")
def get_topology() -> dict:
    with _db() as db:
        row = db.query_one("SELECT * FROM topology_snapshots ORDER BY timestamp DESC LIMIT 1")
    if row is None:
        raise HTTPException(status_code=404, detail="no topology snapshot recorded yet")
    return {"timestamp": row["timestamp"], "source": row["source"], "topology": json.loads(row["topology_json"])}


@app.get("/api/links")
def get_links() -> list[dict]:
    with _db() as db:
        rows = db.query("SELECT * FROM links")
    return _rows_to_dicts(rows)


@app.get("/api/incidents")
def get_incidents(status: str | None = None, limit: int = 50) -> list[dict]:
    with _db() as db:
        if status:
            rows = db.query(
                "SELECT * FROM incidents WHERE status = ? ORDER BY opened_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = db.query("SELECT * FROM incidents ORDER BY opened_at DESC LIMIT ?", (limit,))
    return _rows_to_dicts(rows)


@app.get("/api/incidents/{incident_id}")
def get_incident_detail(incident_id: str) -> dict:
    with _db() as db:
        incident = db.query_one("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        route_changes = db.query(
            "SELECT * FROM route_changes WHERE incident_id = ? ORDER BY timestamp", (incident_id,)
        )
        flow_rules = db.query(
            "SELECT * FROM flow_rules WHERE incident_id = ? ORDER BY timestamp", (incident_id,)
        )
        verifications = db.query(
            "SELECT * FROM verification_results WHERE incident_id = ? ORDER BY timestamp", (incident_id,)
        )
        events = db.query(
            "SELECT * FROM remediation_events WHERE incident_id = ? ORDER BY timestamp", (incident_id,)
        )
    return {
        "incident": dict(incident),
        "route_changes": _rows_to_dicts(route_changes),
        "flow_rules": _rows_to_dicts(flow_rules),
        "verification_results": _rows_to_dicts(verifications),
        "events": _rows_to_dicts(events),
    }


@app.get("/api/route_changes")
def get_route_changes(incident_id: str | None = None, limit: int = 100) -> list[dict]:
    with _db() as db:
        if incident_id:
            rows = db.query(
                "SELECT * FROM route_changes WHERE incident_id = ? ORDER BY timestamp DESC LIMIT ?",
                (incident_id, limit),
            )
        else:
            rows = db.query("SELECT * FROM route_changes ORDER BY timestamp DESC LIMIT ?", (limit,))
    return _rows_to_dicts(rows)


@app.get("/api/telemetry/{link_id}")
def get_telemetry(link_id: str, since: float = 0.0, limit: int = 200) -> list[dict]:
    with _db() as db:
        rows = db.query(
            "SELECT * FROM telemetry WHERE link_id = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
            (link_id, since, limit),
        )
    return _rows_to_dicts(list(reversed(rows)))


@app.get("/api/features/{link_id}")
def get_features(link_id: str, since: float = 0.0, limit: int = 200) -> list[dict]:
    """Derived per-window metrics (common/features.py's utilization/
    loss_rate/latency_ms/error_rate) -- the same values the ML pipeline
    consumes, and the honest source for the dashboard's link-detail
    metrics rather than re-deriving utilization from raw telemetry
    counters client-side."""
    with _db() as db:
        rows = db.query(
            "SELECT * FROM features WHERE link_id = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
            (link_id, since, limit),
        )
    return _rows_to_dicts(list(reversed(rows)))


@app.get("/api/predictions/{link_id}")
def get_predictions(link_id: str, since: float = 0.0, limit: int = 200) -> list[dict]:
    with _db() as db:
        rows = db.query(
            "SELECT * FROM predictions WHERE link_id = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
            (link_id, since, limit),
        )
    return _rows_to_dicts(list(reversed(rows)))


def _new_events_since(last_id: int) -> list[dict]:
    with _db() as db:
        rows = db.query("SELECT * FROM remediation_events WHERE id > ? ORDER BY id", (last_id,))
    return _rows_to_dicts(rows)


def _latest_event_id() -> int:
    with _db() as db:
        row = db.query_one("SELECT MAX(id) as max_id FROM remediation_events")
    return row["max_id"] if row and row["max_id"] is not None else 0


async def _event_stream(poll_interval_s: float = 1.0):
    last_id = await asyncio.to_thread(_latest_event_id)
    while True:
        rows = await asyncio.to_thread(_new_events_since, last_id)
        for row in rows:
            last_id = row["id"]
            yield f"data: {json.dumps(row)}\n\n"
        yield ": keepalive\n\n"
        await asyncio.sleep(poll_interval_s)


@app.get("/api/events/stream")
async def events_stream() -> StreamingResponse:
    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@app.get("/api/results")
def get_results() -> dict:
    """Every measured result artefact, for the dashboard's Results view.

    Reads the JSON files experiments/analyze.py, ml/train_model.py and
    rl/evaluate_rl.py actually wrote. A missing file is reported as
    missing rather than defaulted, so the page can say "not measured
    yet" instead of implying a zero.
    """
    tables = REPO_ROOT / "results" / "tables"
    wanted = {
        "experiments": "experiment_results.json",
        "ml": "ml_metrics.json",
        "ml_threshold_sweep": "ml_threshold_sweep.json",
        "rl_training": "rl_training_summary.json",
        "rl_evaluation": "rl_evaluation.json",
        # The deployment-matched dataset is what the shipped model was
        # trained on; dataset_summary.json describes the superseded
        # Phase 8 one. Showing the wrong one next to the model's metrics
        # would misdescribe where those numbers came from.
        "dataset": "dataset_matched_summary.json",
        "simulator_calibration": "simulator_calibration.json",
    }
    out: dict = {"available": {}, "missing": []}
    for key, filename in wanted.items():
        path = tables / filename
        if not path.exists():
            out["missing"].append(filename)
            continue
        try:
            out["available"][key] = json.loads(path.read_text())
        except ValueError:
            out["missing"].append(filename)

    figures_dir = REPO_ROOT / "results" / "figures"
    out["figures"] = sorted(p.name for p in figures_dir.glob("*.png")) if figures_dir.is_dir() else []
    return out


# Result figures are served from results/figures, separately from the
# dashboard's own static mount so the two cannot shadow each other.
_figures_dir = REPO_ROOT / "results" / "figures"
if _figures_dir.is_dir():
    from fastapi.staticfiles import StaticFiles as _StaticFiles

    app.mount("/figures", _StaticFiles(directory=str(_figures_dir)), name="figures")


# Mounted last, deliberately -- Starlette matches routes in registration
# order and a "/" mount matches almost any path as a prefix, so every
# /api/* route above must be registered first or this would shadow them.
# Phase 20's dashboard (vanilla HTML/CSS/JS, no build step) is served
# directly from this same process rather than a second server.
_dashboard_dir = REPO_ROOT / "dashboard"
if _dashboard_dir.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_dashboard_dir), html=True), name="dashboard")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=_config.api.host, port=_config.api.port)
