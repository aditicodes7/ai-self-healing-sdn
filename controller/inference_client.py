"""HTTP client to service/ml_rl_service.py's degradation-prediction
endpoint, with the mandatory safety gate: if the service is down, times
out, or returns anything but a valid probability, this returns None --
never a fabricated or last-known-good probability substituted silently.
Callers (telemetry_collector.py) must treat None as "no prediction this
round", not as "healthy".

Runs under venv-ctrl (Python 3.9), which has no scikit-learn -- this is
the whole point of the offline-training / online-inference split: the
controller never imports sklearn, it only ever speaks HTTP to a service
that does.
"""
from __future__ import annotations

import time

import requests

from common.logging_setup import configure_logging

logger = configure_logging("controller.inference_client")


class InferenceClient:
    def __init__(self, host: str, port: int, timeout_ms: int):
        self.base_url = f"http://{host}:{port}"
        self.timeout_s = timeout_ms / 1000.0

    def predict_degradation(self, link_id: str, features: dict) -> tuple[float, str] | None:
        """Returns (probability, model_version) on success, or None if the
        service is unreachable, times out, or returns an error -- the
        safety gate. Never raises; a prediction miss is a normal,
        expected outcome the caller must handle, not an exception."""
        t0 = time.perf_counter()
        try:
            resp = requests.post(
                f"{self.base_url}/infer/degradation",
                json={"link_id": link_id, "features": features},
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            logger.warning(
                "inference request failed, falling back to no prediction",
                extra={"link_id": link_id, "error": str(exc)},
            )
            return None

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        if resp.status_code != 200:
            logger.warning(
                "inference service returned non-200, falling back to no prediction",
                extra={"link_id": link_id, "status_code": resp.status_code, "elapsed_ms": elapsed_ms},
            )
            return None

        try:
            body = resp.json()
            probability = float(body["probability"])
            model_version = str(body["model_version"])
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "inference response malformed, falling back to no prediction",
                extra={"link_id": link_id, "error": str(exc)},
            )
            return None

        if not (0.0 <= probability <= 1.0):
            logger.warning(
                "inference returned out-of-range probability, falling back to no prediction",
                extra={"link_id": link_id, "probability": probability},
            )
            return None

        logger.info(
            "degradation prediction",
            extra={
                "link_id": link_id, "probability": probability,
                "model_version": model_version, "elapsed_ms": elapsed_ms,
            },
        )
        return probability, model_version
