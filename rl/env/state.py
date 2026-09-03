"""Builds the fixed-size RL state vector for one route-selection decision:
  src/dst one-hot (2 * num_switches)
  + K candidate-path feature blocks (8 features each, zero-padded when
    fewer than K real candidates exist -- action masking in actions.py
    is what stops the policy from ever choosing a padded slot)
  + global congestion summary (mean/max utilization over all links, 2)
  + time since last reroute (1, normalized to [0, 1])

Uses controller/path_computer.py's LinkMetrics and link_id_for_edge
directly so link identity and cost inputs match the same convention the
heuristic path-cost function uses -- no parallel/inconsistent link-id
scheme.
"""
from __future__ import annotations

import numpy as np

from controller.path_computer import LinkMetrics, link_id_for_edge

N_CANDIDATE_FEATURES = 8


def _candidate_features(
    path: list[int] | None,
    link_metrics: dict[str, LinkMetrics],
    degradation_probs: dict[str, float],
    degraded_link_id: str | None,
    current_path: list[int] | None,
) -> np.ndarray:
    """8 features for one candidate path slot; an all-zero vector for a
    padded (nonexistent) slot -- distinguishable from a real degenerate
    path only via the action mask, never inferred from these values."""
    if not path or len(path) < 2:
        return np.zeros(N_CANDIDATE_FEATURES, dtype=np.float32)

    link_ids = [link_id_for_edge(u, v) for u, v in zip(path, path[1:])]

    utils = [link_metrics.get(lid, LinkMetrics()).utilization for lid in link_ids]
    losses = [link_metrics.get(lid, LinkMetrics()).loss_rate for lid in link_ids]
    latencies = [link_metrics.get(lid, LinkMetrics()).latency_ms for lid in link_ids]
    probs = [degradation_probs.get(lid, 0.0) for lid in link_ids]

    utils = [u for u in utils if u is not None]
    losses = [l for l in losses if l is not None]
    latencies = [lt for lt in latencies if lt is not None]

    return np.array(
        [
            max(utils) if utils else 0.0,
            (sum(utils) / len(utils)) if utils else 0.0,
            max(losses) if losses else 0.0,
            sum(latencies),
            float(len(link_ids)),
            max(probs) if probs else 0.0,
            1.0 if degraded_link_id is not None and degraded_link_id in link_ids else 0.0,
            1.0 if current_path is not None and path == current_path else 0.0,
        ],
        dtype=np.float32,
    )


def state_dim(num_switches: int, k: int) -> int:
    return num_switches * 2 + k * N_CANDIDATE_FEATURES + 2 + 1


def build_state(
    src_dpid: int,
    dst_dpid: int,
    num_switches: int,
    candidates: list[list[int]],
    k: int,
    link_metrics: dict[str, LinkMetrics],
    degradation_probs: dict[str, float],
    degraded_link_id: str | None,
    current_path: list[int] | None,
    time_since_last_reroute_s: float,
    global_mean_utilization: float,
    global_max_utilization: float,
    max_time_since_reroute_s: float = 300.0,
) -> np.ndarray:
    """dpid values are assumed 1-indexed (1..num_switches), matching the
    topology's s1..sN switch numbering used throughout the controller."""
    src_onehot = np.zeros(num_switches, dtype=np.float32)
    dst_onehot = np.zeros(num_switches, dtype=np.float32)
    if 1 <= src_dpid <= num_switches:
        src_onehot[src_dpid - 1] = 1.0
    if 1 <= dst_dpid <= num_switches:
        dst_onehot[dst_dpid - 1] = 1.0

    candidate_block = np.concatenate(
        [
            _candidate_features(
                candidates[i] if i < len(candidates) else None,
                link_metrics,
                degradation_probs,
                degraded_link_id,
                current_path,
            )
            for i in range(k)
        ]
    ) if k > 0 else np.zeros(0, dtype=np.float32)

    global_block = np.array(
        [global_mean_utilization, global_max_utilization], dtype=np.float32
    )
    time_block = np.array(
        [min(max(time_since_last_reroute_s, 0.0) / max_time_since_reroute_s, 1.0)],
        dtype=np.float32,
    )

    return np.concatenate([src_onehot, dst_onehot, candidate_block, global_block, time_block])
