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


@pytest.fixture(autouse=True)
def _load_real_rl_policy():
    ml_rl_service._load_rl_policy()
    if ml_rl_service._rl_model is None:
        pytest.skip(f"models/routing_policy.zip not available: {ml_rl_service._rl_load_error}")
    yield


def _route_request(**overrides):
    defaults = dict(
        src_dpid=1,
        dst_dpid=6,
        candidates=[[1, 2, 3, 6], [1, 4, 5, 6], [1, 3, 6], [1, 2, 5, 6]],
        link_metrics={"s1-s3": {"utilization": 0.9, "latency_ms": 5.0, "loss_rate": 0.1}},
        degradation_probs={"s1-s3": 0.9},
        degraded_link_id="s1-s3",
        current_path=[1, 3, 6],
        time_since_last_reroute_s=0.0,
        global_mean_utilization=0.3,
        global_max_utilization=0.9,
    )
    defaults.update(overrides)
    return ml_rl_service.RouteRequest(**defaults)


def test_health_reports_rl_policy_loaded():
    result = ml_rl_service.health()
    assert result["rl_policy_loaded"] is True


def test_infer_route_returns_valid_action():
    resp = ml_rl_service.infer_route(_route_request())
    assert 0 <= resp.action <= ml_rl_service._rl_k
    assert resp.inference_ms >= 0
    assert resp.policy_version


def test_infer_route_chosen_path_matches_action():
    req = _route_request()
    resp = ml_rl_service.infer_route(req)
    if resp.action == 0:
        assert resp.chosen_path == req.current_path
    else:
        assert resp.chosen_path == req.candidates[resp.action - 1]


def test_infer_route_handles_fewer_candidates_than_k():
    req = _route_request(candidates=[[1, 3, 6]])
    resp = ml_rl_service.infer_route(req)
    assert 0 <= resp.action <= 1  # only action 0 (hold) or 1 (the sole candidate) can be valid


def test_infer_route_raises_503_when_policy_unavailable():
    ml_rl_service._rl_model = None
    ml_rl_service._rl_load_error = "simulated unavailability"
    try:
        with pytest.raises(HTTPException) as exc_info:
            ml_rl_service.infer_route(_route_request())
        assert exc_info.value.status_code == 503
    finally:
        ml_rl_service._load_rl_policy()
