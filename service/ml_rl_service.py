#!/usr/bin/env python3
"""ML/RL inference service. Loads the trained degradation model
(models/degradation_model.joblib + metadata.json, Phase 9) once at
startup and serves POST /infer/degradation over localhost HTTP.
controller/inference_client.py (running under venv-ctrl, which has no
scikit-learn) is the only intended caller -- this is what makes the
offline-training / online-inference separation in the architecture real
rather than nominal.

Runs under venv-ml (Python 3.11, scikit-learn/joblib/fastapi/uvicorn
installed) -- see docs/decisions.md (2026-09-03) for why a second
venv-ml now exists inside the VM alongside venv-ctrl: this service must
be reachable via localhost from the controller, and the controller runs
in the VM.

RL route inference (Phase 18's POST /infer/route) is added to this same
service later, per the repo layout -- one process serving both trained
artefacts, not two.

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

logger = configure_logging("service.ml_rl_service")

_model = None
_metadata: dict | None = None
_load_error: str | None = None


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_model()
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


@app.get("/health")
def health():
    return {
        "status": "ok" if _model is not None else "model_unavailable",
        "model_loaded": _model is not None,
        "load_error": _load_error,
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


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(app, host=cfg.ml_rl_service.host, port=cfg.ml_rl_service.port)
