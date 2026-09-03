"""HTTP client to service/ml_rl_service.py's RL route-selection endpoint
(POST /infer/route), with the same mandatory safety gate as
inference_client.py: if the service is down, times out, or returns
anything that isn't a usable action, this returns None -- never a
fabricated or last-known-good route substituted silently. Callers
(remediation_manager.py) must treat None as "no RL decision this round"
and fall back to the weighted-cost heuristic, logging rl_fallback=true.

Runs under venv-ctrl (Python 3.9), which has neither scikit-learn nor
stable-baselines3 -- same offline-training / online-inference split as
inference_client.py.
"""
from __future__ import annotations

import dataclasses
import time

import requests

from common.logging_setup import configure_logging
from controller.path_computer import LinkMetrics

logger = configure_logging("controller.rl_client")


@dataclasses.dataclass(frozen=True)
class RouteDecision:
    action: int
    chosen_path: list[int] | None
    policy_version: str


class RLClient:
    def __init__(self, host: str, port: int, timeout_ms: int):
        self.base_url = f"http://{host}:{port}"
        self.timeout_s = timeout_ms / 1000.0

    def select_route(
        self,
        src_dpid: int,
        dst_dpid: int,
        candidates: list[list[int]],
        link_metrics: dict[str, LinkMetrics],
        degradation_probs: dict[str, float],
        degraded_link_id: str | None,
        current_path: list[int] | None,
        time_since_last_reroute_s: float,
        global_mean_utilization: float,
        global_max_utilization: float,
    ) -> RouteDecision | None:
        """Returns a RouteDecision on success, or None if the service is
        unreachable, times out, or returns something unusable -- the
        safety gate. Never raises; a decision miss is a normal, expected
        outcome the caller must handle, not an exception."""
        payload = {
            "src_dpid": src_dpid,
            "dst_dpid": dst_dpid,
            "candidates": candidates,
            "link_metrics": {
                lid: {"utilization": m.utilization, "latency_ms": m.latency_ms, "loss_rate": m.loss_rate}
                for lid, m in link_metrics.items()
            },
            "degradation_probs": degradation_probs,
            "degraded_link_id": degraded_link_id,
            "current_path": current_path,
            "time_since_last_reroute_s": time_since_last_reroute_s,
            "global_mean_utilization": global_mean_utilization,
            "global_max_utilization": global_max_utilization,
        }

        t0 = time.perf_counter()
        try:
            resp = requests.post(f"{self.base_url}/infer/route", json=payload, timeout=self.timeout_s)
        except requests.RequestException as exc:
            logger.warning(
                "RL route request failed, falling back to heuristic",
                extra={"src_dpid": src_dpid, "dst_dpid": dst_dpid, "error": str(exc)},
            )
            return None

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        if resp.status_code != 200:
            logger.warning(
                "RL service returned non-200, falling back to heuristic",
                extra={
                    "src_dpid": src_dpid, "dst_dpid": dst_dpid,
                    "status_code": resp.status_code, "elapsed_ms": elapsed_ms,
                },
            )
            return None

        try:
            body = resp.json()
            action = int(body["action"])
            chosen_path = body["chosen_path"]
            policy_version = str(body["policy_version"])
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "RL response malformed, falling back to heuristic",
                extra={"src_dpid": src_dpid, "dst_dpid": dst_dpid, "error": str(exc)},
            )
            return None

        if action < 0 or action > len(candidates):
            logger.warning(
                "RL returned an action index out of range, falling back to heuristic",
                extra={"src_dpid": src_dpid, "dst_dpid": dst_dpid, "action": action, "n_candidates": len(candidates)},
            )
            return None

        logger.info(
            "RL route decision",
            extra={
                "src_dpid": src_dpid, "dst_dpid": dst_dpid, "action": action,
                "chosen_path": chosen_path, "elapsed_ms": elapsed_ms,
            },
        )
        return RouteDecision(action=action, chosen_path=chosen_path, policy_version=policy_version)
