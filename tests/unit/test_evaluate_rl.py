import numpy as np
import pytest
import yaml

from rl.env.network_env import EnvConfig, SDNRoutingEnv
from rl.evaluate_rl import _least_utilized_action, _mean_ci95, _summarize

TOPOLOGY_PATH = "network/topologies/scenario_a.yaml"


@pytest.fixture
def topology_spec():
    with open(TOPOLOGY_PATH) as f:
        return yaml.safe_load(f)


@pytest.fixture
def env(topology_spec):
    cfg = EnvConfig(episode_steps=20, fault_onset_step_min=3, fault_onset_step_max=5)
    e = SDNRoutingEnv(topology_spec, env_config=cfg, src=1, dst=6, seed=1)
    e.reset(seed=1)
    return e


def test_least_utilized_action_picks_lowest_utilization_candidate(env):
    # Fabricate link_results where every link on candidates[1] is far
    # less utilized than everything else -- the greedy heuristic must
    # select that candidate's action index (2), not hold or another.
    link_results = {}
    for lid in env.sim.links:
        link_results[lid] = {"utilization": 0.9, "latency_ms": 1.0, "loss_rate": 0.0}
    target_path = env.candidates[1]
    for u, v in zip(target_path, target_path[1:]):
        from controller.path_computer import link_id_for_edge

        link_results[link_id_for_edge(u, v)] = {"utilization": 0.01, "latency_ms": 1.0, "loss_rate": 0.0}

    action = _least_utilized_action(env, link_results)
    assert action == 2


def test_least_utilized_action_respects_mask(env):
    # k=8 with only 4 real candidates -- padded slots must never be
    # selected even if their (default-zero) utilization looks best.
    cfg = EnvConfig(episode_steps=20, k_shortest=8)
    padded_env = SDNRoutingEnv(env.topology_spec, env_config=cfg, src=1, dst=6, seed=1)
    padded_env.reset(seed=1)
    link_results = {lid: {"utilization": 0.5, "latency_ms": 1.0, "loss_rate": 0.0} for lid in padded_env.sim.links}
    action = _least_utilized_action(padded_env, link_results)
    mask = padded_env.action_masks()
    assert mask[action]


def test_mean_ci95_single_value_has_zero_width():
    result = _mean_ci95(np.array([5.0]))
    assert result["mean"] == 5.0
    assert result["ci95_half_width"] == 0.0
    assert result["n"] == 1


def test_mean_ci95_matches_known_values():
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = _mean_ci95(values)
    assert result["mean"] == 3.0
    assert result["n"] == 5
    assert result["ci95_half_width"] > 0


def test_summarize_recovery_rate_ignores_episodes_with_no_fault():
    episode_results = [
        {"episode_reward": 1.0, "reroute_count": 1, "mean_post_reroute_max_utilization": 0.5, "recovery_success": True},
        {"episode_reward": 2.0, "reroute_count": 0, "mean_post_reroute_max_utilization": None, "recovery_success": False},
        {"episode_reward": 3.0, "reroute_count": 2, "mean_post_reroute_max_utilization": 0.3, "recovery_success": None},
    ]
    summary = _summarize(episode_results)
    assert summary["recovery_success_n"] == 2
    assert summary["recovery_success_rate"] == 0.5
    assert summary["episode_reward"]["n"] == 3
    assert summary["mean_post_reroute_max_utilization"]["n"] == 2


def test_summarize_all_none_recovery_gives_none_rate():
    episode_results = [
        {"episode_reward": 1.0, "reroute_count": 0, "mean_post_reroute_max_utilization": None, "recovery_success": None},
    ]
    summary = _summarize(episode_results)
    assert summary["recovery_success_rate"] is None
    assert summary["mean_post_reroute_max_utilization"] is None
