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
from network.failure_injector import FAULT_TYPES  # noqa: E402

logger = configure_logging("backend.api")

app = FastAPI(title="ai-self-healing-sdn backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_config = load_config()
_db_path = REPO_ROOT / _config.database.path

# Fault requests from the dashboard are appended here as JSON lines and
# picked up by demo/run_live_demo.py --interactive, which owns the Mininet
# network. A file, not a socket or a queue: the injector only exists inside
# the demo process (Mininet needs root and the live net object), the backend
# runs separately under venv-ml, and a broker is outside the approved stack.
# It sits beside the database so it follows whichever working directory the
# demo configured, rather than being hardcoded.
_commands_path = _db_path.parent / "fault_commands.jsonl"

# A dashboard-triggered fault recovers itself after this long, so a demo
# cannot be left with a permanently broken link.
_INJECT_MIN_S, _INJECT_MAX_S, _INJECT_DEFAULT_S = 30.0, 600.0, 90.0


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _db() -> Database:
    return Database(_db_path, wal=_config.database.wal)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "time": time.time()}


def _active_fault() -> tuple[str | None, float]:
    """The link currently under a fault, and the seconds left on it.

    Only one fault may be live at a time. Overlapping faults were observed
    to defeat detection entirely: the features are ratios against each
    link's own recent baseline, so once several links are throttled at once
    the baselines are themselves degraded and nothing looks anomalous --
    one measured case peaked at P=0.226, below even the abstain band.

    Reads the queue the demo consumes, so it holds however the request
    arrived: the dashboard, a second browser tab, or curl.
    """
    if not _commands_path.exists():
        return None, 0.0
    now = time.time()
    busiest: tuple[str | None, float] = (None, 0.0)
    cancelled: set[str] = set()
    for line in _commands_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        link_id = str(cmd.get("link_id"))
        if cmd.get("action") == "recover":
            # An explicit recovery cancels whatever was live on that link.
            cancelled.add(link_id)
            continue
        remaining = (float(cmd.get("requested_at", 0.0))
                     + float(cmd.get("duration_s", _INJECT_DEFAULT_S)) - now)
        if remaining > 0 and remaining > busiest[1]:
            busiest = (link_id, remaining)
    if busiest[0] in cancelled:
        return None, 0.0
    return busiest


@app.get("/api/fault_options")
def fault_options() -> dict:
    """What the dashboard may offer for a manual break.

    `interactive` is False unless the demo has created the command queue,
    so the dashboard can hide the control rather than offer a button that
    silently does nothing.
    """
    with _db() as db:
        rows = db.query("SELECT DISTINCT link_id FROM links ORDER BY link_id")
    return {
        "links": [r["link_id"] for r in rows],
        "fault_types": list(FAULT_TYPES),
        "interactive": _commands_path.exists(),
        "default_duration_s": _INJECT_DEFAULT_S,
    }


@app.post("/api/inject")
def inject_fault(payload: dict) -> dict:
    """Queue a fault for the demo process to apply.

    Validates against the links the controller has actually discovered, so
    a typo or a stale dashboard cannot ask for a link that does not exist.
    Returns 409 rather than 404 when the queue is missing: the request is
    well-formed, the demo just is not running in interactive mode.
    """
    if not _commands_path.exists():
        raise HTTPException(
            status_code=409,
            detail="no interactive demo is running (start it with --interactive)",
        )

    link_id = str(payload.get("link_id", "")).strip()
    fault_type = str(payload.get("fault_type", "gradual_degradation")).strip()
    try:
        duration_s = float(payload.get("duration_s", _INJECT_DEFAULT_S))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="duration_s must be a number")

    if fault_type not in FAULT_TYPES:
        raise HTTPException(
            status_code=422, detail=f"unknown fault_type {fault_type!r}; expected one of {list(FAULT_TYPES)}")
    if not (_INJECT_MIN_S <= duration_s <= _INJECT_MAX_S):
        raise HTTPException(
            status_code=422,
            detail=f"duration_s must be between {_INJECT_MIN_S:.0f} and {_INJECT_MAX_S:.0f}")

    with _db() as db:
        known = db.query_one("SELECT link_id FROM links WHERE link_id = ?", (link_id,))
    if known is None:
        raise HTTPException(status_code=422, detail=f"unknown link_id {link_id!r}")

    # Reject a repeat request for a link that is already broken. Observed
    # in testing: a double-click 0.4s apart queued the same fault twice,
    # and four overlapping faults on one link is not something to discover
    # in front of an audience.
    busy_link, remaining = _active_fault()
    if busy_link is not None:
        same = busy_link == link_id
        raise HTTPException(
            status_code=409,
            detail=(f"{busy_link} is still broken ({remaining:.0f}s left). "
                    + ("Wait for it to recover, then break it again."
                       if same else
                       "Breaking a second link at the same time destroys the "
                       "baselines the model compares against, so one at a time.")),
        )

    command = {
        "requested_at": time.time(),
        "link_id": link_id,
        "fault_type": fault_type,
        "duration_s": duration_s,
    }
    with _commands_path.open("a") as fh:
        fh.write(json.dumps(command) + "\n")
    logger.info("fault requested from the dashboard", extra=command)
    return {"accepted": True, **command}


@app.post("/api/recover")
def recover_fault(payload: dict) -> dict:
    """Restore a link immediately, instead of waiting out its duration.

    A fault recovers itself, but during a live demonstration a wrong pick
    should not cost minutes of waiting -- and nothing else can be broken
    until the current one clears.
    """
    if not _commands_path.exists():
        raise HTTPException(status_code=409, detail="no interactive demo is running")

    link_id = str(payload.get("link_id", "")).strip()
    busy_link, _ = _active_fault()
    if not link_id:
        link_id = busy_link or ""
    if not link_id:
        raise HTTPException(status_code=409, detail="nothing is broken right now")

    command = {"requested_at": time.time(), "link_id": link_id, "action": "recover"}
    with _commands_path.open("a") as fh:
        fh.write(json.dumps(command) + "\n")
    logger.info("recovery requested from the dashboard", extra=command)
    return {"accepted": True, **command}


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


@app.get("/api/experiment_plan")
def get_experiment_plan() -> dict:
    """What each experiment is meant to prove, and whether it has been
    measured yet.

    The plan half is read from experiments/definitions.py rather than
    restated here, so the dashboard cannot drift from the experiments
    that actually run. The status half keys off
    results/tables/experiment_results.json -- the analysed output --
    rather than counting rows in experiments.db. That distinction is
    deliberate: the raw table still holds trials from superseded runs
    (an E2 attempt whose baseline arm ran CPU-starved, see
    docs/limitations.md), and counting those would present discarded
    work as evidence. An experiment reports as measured only once
    analyze.py has produced numbers for it.
    """
    import dataclasses

    from experiments.definitions import EXPERIMENTS

    analysed_path = REPO_ROOT / "results" / "tables" / "experiment_results.json"
    analysed: dict = {}
    if analysed_path.exists():
        try:
            analysed = json.loads(analysed_path.read_text()).get("results", {})
        except ValueError:
            analysed = {}

    plan = []
    for exp in EXPERIMENTS:
        d = dataclasses.asdict(exp)
        prefix = f"{d['experiment_id']}/"
        measured = {k.split("/", 1)[1]: v for k, v in analysed.items() if k.startswith(prefix)}
        plan.append({
            "experiment_id": d["experiment_id"],
            "title": d["title"],
            "claim": d["claim"],
            "systems": list(d["systems"]),
            "faults": list(d["faults"]),
            "notes": d.get("notes") or "",
            "trials_planned": len(d["systems"]) * len(d["faults"]) * d["seeds_required"],
            "status": "measured" if measured else "not_yet_run",
            "trials_analysed": sum(v.get("trials_usable", 0) for v in measured.values()),
        })
    return {"experiments": plan}


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
