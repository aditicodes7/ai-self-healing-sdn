import numpy as np
import pytest
import yaml

from rl.env.network_env import EnvConfig, SDNRoutingEnv

TOPOLOGY_PATH = "network/topologies/scenario_a.yaml"


@pytest.fixture
def topology_spec():
    with open(TOPOLOGY_PATH) as f:
        return yaml.safe_load(f)


def _make_env(topology_spec, **overrides):
    defaults = dict(episode_steps=20, fault_onset_step_min=3, fault_onset_step_max=5)
    defaults.update(overrides)
    cfg = EnvConfig(**defaults)
    return SDNRoutingEnv(topology_spec, env_config=cfg, src=1, dst=6, seed=123)


def test_reset_returns_observation_matching_space(topology_spec):
    env = _make_env(topology_spec)
    obs, info = env.reset(seed=1)
    assert env.observation_space.contains(obs)
    assert info == {}


def test_action_mask_matches_candidate_count(topology_spec):
    env = _make_env(topology_spec)
    env.reset(seed=1)
    mask = env.action_masks()
    assert mask[0] == True  # noqa: E712
    assert mask.sum() == 1 + len(env.candidates)


def test_step_returns_valid_shapes(topology_spec):
    env = _make_env(topology_spec)
    env.reset(seed=1)
    obs, reward, terminated, truncated, info = env.step(0)
    assert env.observation_space.contains(obs)
    assert isinstance(reward, float)
    assert terminated is False
    assert truncated is False
    assert "path_changed" in info


def test_episode_truncates_at_episode_steps(topology_spec):
    env = _make_env(topology_spec, episode_steps=5)
    env.reset(seed=1)
    truncated = False
    steps = 0
    while not truncated and steps < 100:
        _, _, terminated, truncated, _ = env.step(0)
        steps += 1
    assert truncated
    assert steps == 5


def test_fault_is_injected_on_current_path_link(topology_spec):
    env = _make_env(topology_spec)
    env.reset(seed=7)
    initial_path = env.current_path
    for _ in range(env.fault_onset_step + 1):
        env.step(0)  # action 0 = hold, so current_path stays == initial_path throughout
    assert env.fault_active
    assert env.fault_link_id is not None
    initial_path_link_ids = {
        f"s{a}-s{b}" if a < b else f"s{b}-s{a}" for a, b in zip(initial_path, initial_path[1:])
    }
    assert env.fault_link_id in initial_path_link_ids


def test_rerouting_away_from_fault_recovers(topology_spec):
    env = _make_env(topology_spec, base_offered_load_bps=4_000_000.0)
    env.reset(seed=7)
    for _ in range(env.fault_onset_step + 1):
        obs, reward, term, trunc, info = env.step(0)

    assert env.fault_active
    mask = env.action_masks()
    other_action = next(
        (a for a in range(1, env.k + 1) if mask[a] and env.candidates[a - 1] != env.current_path),
        None,
    )
    assert other_action is not None
    obs, reward, term, trunc, info = env.step(other_action)
    assert info["path_changed"] is True


def test_invalid_action_falls_back_to_no_op(topology_spec):
    # k_shortest=8 deliberately exceeds the 4 real simple paths this
    # topology has between s1 and s6, guaranteeing padded (invalid) slots.
    env = _make_env(topology_spec, k_shortest=8)
    env.reset(seed=1)
    mask = env.action_masks()
    invalid_actions = [a for a in range(env.action_space.n) if not mask[a]]
    assert invalid_actions, "expected at least one padded/invalid action slot"
    obs, reward, term, trunc, info = env.step(invalid_actions[0])
    assert info["path_changed"] is False


def test_link_failure_fault_never_produces_inf_or_nan(topology_spec):
    # NetworkSimulator reports latency_ms=inf for a down link (correct at
    # the simulator layer -- "queueing delay undefined") but that must
    # never reach the policy's observation or the reward as inf/nan.
    env = _make_env(topology_spec)
    env.reset(seed=1)
    down_link = next(iter(env.sim.links))
    env.sim.inject_fault(down_link, down=True)
    env.fault_link_id = down_link
    env.fault_active = True

    obs, reward, term, trunc, info = env.step(0)
    assert np.all(np.isfinite(obs))
    assert np.isfinite(reward)


def test_reset_is_deterministic_given_seed(topology_spec):
    env1 = _make_env(topology_spec)
    env2 = _make_env(topology_spec)
    obs1, _ = env1.reset(seed=42)
    obs2, _ = env2.reset(seed=42)
    assert np.allclose(obs1, obs2)
    assert env1.fault_onset_step == env2.fault_onset_step
    assert env1.candidates == env2.candidates
