#!/usr/bin/env python3
"""ML/RL inference service. Loads the trained degradation model
(models/degradation_model.joblib + metadata.json, Phase 9) and the
trained RL route-selection policy (models/routing_policy.zip, Phase 17)
once at startup and serves POST /infer/degradation and POST /infer/route
over localhost HTTP. controller/inference_client.py and
controller/rl_client.py (running under venv-ctrl, which has neither
scikit-learn nor stable-baselines3) are the only intended callers --
this is what makes the offline-training / online-inference separation
in the architecture real rather than nominal.

Runs under venv-ml (Python 3.11, scikit-learn/joblib/fastapi/uvicorn/
sb3-contrib installed) -- see docs/decisions.md (2026-09-03) for why a
second venv-ml now exists inside the VM alongside venv-ctrl: this
service must be reachable via localhost from the controller, and the
controller runs in the VM.

POST /infer/route reuses rl/env/state.py's build_state() and
rl/env/actions.py's get_action_mask() -- the SAME functions
rl/train_rl.py's environment used to build the policy's training
observations -- so a live decision is built from an identical state
representation to what the policy was trained on (the same train/live
-equality principle Phase 6 established for the ML feature pipeline).

Run with: venv-ml/bin/uvicorn service.ml_rl_service:app --host <cfg> --port <cfg>
"""
from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import joblib
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.config_loader import load_config  # noqa: E402
from common.logging_setup import configure_logging  # noqa: E402
from controller.path_computer import LinkMetrics  # noqa: E402
from rl.env.actions import action_to_path, get_action_mask  # noqa: E402
from rl.env.state import build_state, state_dim  # noqa: E402

logger = configure_logging("service.ml_rl_service")

_model = None
_metadata: dict | None = None
_load_error: str | None = None

_rl_model = None
_rl_load_error: str | None = None
_rl_k: int | None = None
_rl_num_switches: int | None = None


def _load_model() -> None:
    global _model, _metadata, _load_error
    config = load_config()
    model_path = REPO_ROOT / config.ml.model_path
    metadata_path = REPO_ROOT / config.ml.metadata_path

    try:
        import json

        _metadata = json.loads(metadata_path.read_text())
        _model = joblib.load(model_path)
        logger.info(
            "degradation model loaded",
            extra={"model": _metadata.get("chosen_model"), "path": str(model_path)},
        )
    except Exception as exc:
        _load_error = str(exc)
        logger.exception("failed to load degradation model", extra={"path": str(model_path)})


def _load_rl_policy() -> None:
    global _rl_model, _rl_load_error, _rl_k, _rl_num_switches
    config = load_config()
    policy_path = REPO_ROOT / config.rl.policy_path

    try:
        from network.topo_builder import load_topology_spec
        from sb3_contrib import MaskablePPO

        _rl_model = MaskablePPO.load(str(policy_path))
        _rl_k = config.path.k_shortest
        topology_spec = load_topology_spec(config.network.topology_name)
        _rl_num_switches = len(topology_spec["switches"])
        logger.info("RL policy loaded", extra={"path": str(policy_path), "k": _rl_k})
    except Exception as exc:
        _rl_load_error = str(exc)
        logger.exception("failed to load RL policy", extra={"path": str(policy_path)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_model()
    _load_rl_policy()
    yield


app = FastAPI(title="ml_rl_service", lifespan=lifespan)


class DegradationRequest(BaseModel):
    link_id: str
    features: dict[str, float | None]


class DegradationResponse(BaseModel):
    link_id: str
    probability: float
    model_version: str
    threshold: float
    inference_ms: float


class DegradationBatchItem(BaseModel):
    link_id: str
    features: dict[str, float | None]


class DegradationBatchRequest(BaseModel):
    items: list[DegradationBatchItem]


class DegradationBatchResult(BaseModel):
    link_id: str
    probability: float


class DegradationBatchResponse(BaseModel):
    results: list[DegradationBatchResult]
    model_version: str
    threshold: float
    inference_ms: float


class RouteRequest(BaseModel):
    src_dpid: int
    dst_dpid: int
    candidates: list[list[int]]
    link_metrics: dict[str, dict[str, float | None]]
    degradation_probs: dict[str, float]
    degraded_link_id: str | None
    current_path: list[int] | None
    time_since_last_reroute_s: float
    global_mean_utilization: float
    global_max_utilization: float
    # Defaults True so an older client keeps its existing behaviour; the
    # controller sends False when it is remediating an open incident.
    allow_hold: bool = True


class RouteResponse(BaseModel):
    action: int
    chosen_path: list[int] | None
    policy_version: str
    inference_ms: float


@app.get("/health")
def health():
    return {
        "status": "ok" if _model is not None else "model_unavailable",
        "model_loaded": _model is not None,
        "load_error": _load_error,
        "rl_policy_loaded": _rl_model is not None,
        "rl_load_error": _rl_load_error,
    }


@app.post("/infer/degradation", response_model=DegradationResponse)
def infer_degradation(req: DegradationRequest):
    if _model is None or _metadata is None:
        # Never fabricate a probability when the model isn't loaded --
        # the caller (inference_client.py) is required to treat a
        # non-2xx response as "no prediction available" and fall back
        # accordingly, never substitute a guessed value.
        raise HTTPException(status_code=503, detail=f"model not loaded: {_load_error}")

    feature_order = _metadata["feature_order"]
    row = np.full((1, len(feature_order)), np.nan, dtype=float)
    for j, name in enumerate(feature_order):
        v = req.features.get(name)
        if v is not None:
            row[0, j] = v

    t0 = time.perf_counter()
    probability = float(_model.predict_proba(row)[0, 1])
    inference_ms = (time.perf_counter() - t0) * 1000.0

    config = load_config()
    return DegradationResponse(
        link_id=req.link_id,
        probability=probability,
        model_version=_metadata.get("chosen_model", "unknown"),
        threshold=config.ml.threshold,
        inference_ms=inference_ms,
    )


@app.post("/infer/degradation/batch", response_model=DegradationBatchResponse)
def infer_degradation_batch(req: DegradationBatchRequest):
    """Scores every link in ONE request and one predict_proba call.

    Phase 21 measured that issuing a separate HTTP inference call per
    link, from inside the controller's single-threaded event loop,
    inflated the controller's own latency-probe measurements ~20x (23 ms
    -> 464 ms) -- which corrupts the latency-slope features this very
    model depends on. Batching collapses ~16 round trips per telemetry
    cycle into one, and sklearn scores a matrix far faster than N
    single rows. See docs/limitations.md.
    """
    if _model is None or _metadata is None:
        raise HTTPException(status_code=503, detail=f"model not loaded: {_load_error}")

    feature_order = _metadata["feature_order"]
    if not req.items:
        return DegradationBatchResponse(
            results=[], model_version=_metadata.get("chosen_model", "unknown"),
            threshold=load_config().ml.threshold, inference_ms=0.0,
        )

    matrix = np.full((len(req.items), len(feature_order)), np.nan, dtype=float)
    for i, item in enumerate(req.items):
        for j, name in enumerate(feature_order):
            v = item.features.get(name)
            if v is not None:
                matrix[i, j] = v

    t0 = time.perf_counter()
    probabilities = _model.predict_proba(matrix)[:, 1]
    inference_ms = (time.perf_counter() - t0) * 1000.0

    return DegradationBatchResponse(
        results=[
            DegradationBatchResult(link_id=item.link_id, probability=float(p))
            for item, p in zip(req.items, probabilities)
        ],
        model_version=_metadata.get("chosen_model", "unknown"),
        threshold=load_config().ml.threshold,
        inference_ms=inference_ms,
    )


@app.post("/infer/route", response_model=RouteResponse)
def infer_route(req: RouteRequest):
    if _rl_model is None or _rl_k is None or _rl_num_switches is None:
        # Same contract as /infer/degradation: never fabricate a route
        # choice when the policy isn't loaded -- controller/rl_client.py
        # must treat a non-2xx response as "no RL decision available" and
        # fall back to the weighted-cost heuristic, never guess an action.
        raise HTTPException(status_code=503, detail=f"RL policy not loaded: {_rl_load_error}")

    link_metrics = {
        link_id: LinkMetrics(
            utilization=m.get("utilization"),
            latency_ms=m.get("latency_ms"),
            loss_rate=m.get("loss_rate"),
        )
        for link_id, m in req.link_metrics.items()
    }

    t0 = time.perf_counter()
    state = build_state(
        src_dpid=req.src_dpid,
        dst_dpid=req.dst_dpid,
        num_switches=_rl_num_switches,
        candidates=req.candidates,
        k=_rl_k,
        link_metrics=link_metrics,
        degradation_probs=req.degradation_probs,
        degraded_link_id=req.degraded_link_id,
        current_path=req.current_path,
        time_since_last_reroute_s=req.time_since_last_reroute_s,
        global_mean_utilization=req.global_mean_utilization,
        global_max_utilization=req.global_max_utilization,
    )
    if state.shape != (state_dim(_rl_num_switches, _rl_k),) or not np.all(np.isfinite(state)):
        # Defense in depth: a malformed request (wrong topology size,
        # candidates that don't match this policy's K) must never reach
        # the network as a silently-wrong action -- fail loudly instead.
        raise HTTPException(status_code=422, detail="constructed state vector is malformed or non-finite")

    mask = get_action_mask(len(req.candidates), _rl_k, allow_hold=req.allow_hold)
    action, _ = _rl_model.predict(state, action_masks=mask, deterministic=True)
    action = int(action)
    inference_ms = (time.perf_counter() - t0) * 1000.0

    chosen_path = action_to_path(action, req.candidates, req.current_path) if mask[action] else None

    logger.info(
        "route prediction",
        extra={
            "src_dpid": req.src_dpid, "dst_dpid": req.dst_dpid, "action": action,
            "chosen_path": chosen_path, "inference_ms": inference_ms,
        },
    )
    return RouteResponse(
        action=action,
        chosen_path=chosen_path,
        policy_version=Path(load_config().rl.policy_path).name,
        inference_ms=inference_ms,
    )


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(app, host=cfg.ml_rl_service.host, port=cfg.ml_rl_service.port)
