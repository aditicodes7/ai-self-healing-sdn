"""Unit tests for service/ml_rl_service.py's inference logic, called
directly as plain functions (no HTTP layer) -- real end-to-end HTTP
behavior against controller/inference_client.py is verified separately
against the actually-running service (see PROJECT_STATE.md Phase 10).
"""
import pytest
from fastapi import HTTPException

from service import ml_rl_service


@pytest.fixture(autouse=True)
def _load_real_model():
    ml_rl_service._load_model()
    if ml_rl_service._model is None:
        pytest.skip(f"models/degradation_model.joblib not available: {ml_rl_service._load_error}")
    yield


def test_health_reports_model_loaded():
    result = ml_rl_service.health()
    assert result["model_loaded"] is True
    assert result["status"] == "ok"


def test_infer_degradation_returns_valid_probability():
    feature_order = ml_rl_service._metadata["feature_order"]
    features = {name: 0.1 for name in feature_order}
    req = ml_rl_service.DegradationRequest(link_id="s1-s2", features=features)

    resp = ml_rl_service.infer_degradation(req)

    assert resp.link_id == "s1-s2"
    assert 0.0 <= resp.probability <= 1.0
    assert resp.model_version == ml_rl_service._metadata["chosen_model"]
    assert resp.inference_ms >= 0


def test_infer_degradation_handles_missing_features_as_nan():
    # Partial feature dict (as a live controller might send before enough
    # history has accumulated) must not crash -- missing entries become
    # NaN, matching common/features.py's own "None means not yet
    # computable" policy, not a fabricated 0.
    req = ml_rl_service.DegradationRequest(link_id="s1-s2", features={})
    resp = ml_rl_service.infer_degradation(req)
    assert 0.0 <= resp.probability <= 1.0


def test_infer_degradation_raises_503_when_model_unavailable():
    ml_rl_service._model = None
    ml_rl_service._load_error = "simulated unavailability"
    try:
        with pytest.raises(HTTPException) as exc_info:
            ml_rl_service.infer_degradation(
                ml_rl_service.DegradationRequest(link_id="s1-s2", features={})
            )
        assert exc_info.value.status_code == 503
    finally:
        ml_rl_service._load_model()
