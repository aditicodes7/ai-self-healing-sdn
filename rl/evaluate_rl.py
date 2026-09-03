#!/usr/bin/env python3
"""Evaluates the trained MaskablePPO route-selection policy
(rl/train_rl.py's output) against three baselines over >=eval_episodes
seeded episodes of rl/env/network_env.py's simulator-backed environment
(Phase 17's acceptance gate). Every reported number is the actual mean/
95% CI computed from these executed episodes -- results/tables/
rl_evaluation.json is the traceable artefact, never an estimate.

Baselines:
  learned                -- the trained MaskablePPO policy (deterministic)
  random_valid           -- uniform random choice among currently-valid
                             actions (the mask); a lower bound
  always_shortest_path   -- always selects the single globally-shortest
                             candidate (hop-count), regardless of live
                             conditions -- mirrors this project's B1
                             static-shortest-path baseline
                             (controller/baselines/shortest_path.py)
  always_least_utilized  -- greedy: each step, picks whichever currently
                             -valid option (hold or a candidate) has the
                             lowest max-link-utilization right now, no
                             persistence/fault-awareness -- mirrors B2's
                             reactive-threshold spirit without the
                             threshold/hysteresis machinery
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sb3_contrib import MaskablePPO

from common.config_loader import load_config
from controller.path_computer import link_id_for_edge
from network.topo_builder import load_topology_spec
from rl.env.network_env import EnvConfig, SDNRoutingEnv
from rl.env.rewards import RewardWeights

BASELINE_NAMES = ("learned", "random_valid", "always_shortest_path", "always_least_utilized")


def _least_utilized_action(env: SDNRoutingEnv, link_results: dict) -> int:
    mask = env.action_masks()
    options = {0: env.current_path}
    for i, path in enumerate(env.candidates):
        options[i + 1] = path
    best_action, best_util = 0, None
    for action, path in options.items():
        if action != 0 and not mask[action]:
            continue
        link_ids = [link_id_for_edge(u, v) for u, v in zip(path, path[1:])]
        utils = [link_results.get(lid, {}).get("utilization", 0.0) for lid in link_ids]
        max_util = max(utils) if utils else 0.0
        if best_util is None or max_util < best_util:
            best_action, best_util = action, max_util
    return best_action


def _pick_action(policy_name: str, env: SDNRoutingEnv, obs: np.ndarray, model, rng: np.random.Generator) -> int:
    mask = env.action_masks()
    if policy_name == "learned":
        action, _ = model.predict(obs, action_masks=mask, deterministic=True)
        return int(action)
    if policy_name == "random_valid":
        valid = np.flatnonzero(mask)
        return int(rng.choice(valid))
    if policy_name == "always_shortest_path":
        return 1 if mask[1] else 0
    if policy_name == "always_least_utilized":
        link_results = env.sim.step()["links"]
        return _least_utilized_action(env, link_results)
    raise ValueError(f"unknown policy {policy_name!r}")


def run_episode(env: SDNRoutingEnv, policy_name: str, model, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    obs, _ = env.reset(seed=seed)
    total_reward = 0.0
    reroute_count = 0
    post_reroute_utils: list[float] = []
    recovery_success_at_end = False
    fault_seen = False
    terminated = truncated = False
    while not (terminated or truncated):
        action = _pick_action(policy_name, env, obs, model, rng)
        obs, reward, terminated, truncated, info = env.step(action)
        total_reward += reward
        if info["path_changed"]:
            reroute_count += 1
            utils = [r["utilization"] for r in env.sim.step()["links"].values()]
            post_reroute_utils.append(max(utils) if utils else 0.0)
        if info["fault_active"]:
            fault_seen = True
            recovery_success_at_end = info["recovery_success"]

    return {
        "episode_reward": total_reward,
        "reroute_count": reroute_count,
        "mean_post_reroute_max_utilization": (
            float(np.mean(post_reroute_utils)) if post_reroute_utils else None
        ),
        "recovery_success": recovery_success_at_end if fault_seen else None,
    }


def _mean_ci95(values: np.ndarray) -> dict:
    n = len(values)
    mean = float(np.mean(values))
    std = float(np.std(values, ddof=1)) if n > 1 else 0.0
    half_width = 1.96 * std / np.sqrt(n) if n > 1 else 0.0
    return {"mean": mean, "std": std, "ci95_half_width": half_width, "n": n}


def _summarize(episode_results: list[dict]) -> dict:
    rewards = np.array([e["episode_reward"] for e in episode_results])
    reroutes = np.array([e["reroute_count"] for e in episode_results])
    recoveries = [e["recovery_success"] for e in episode_results if e["recovery_success"] is not None]
    post_reroute_utils = [
        e["mean_post_reroute_max_utilization"]
        for e in episode_results
        if e["mean_post_reroute_max_utilization"] is not None
    ]

    return {
        "episode_reward": _mean_ci95(rewards),
        "reroute_count": _mean_ci95(reroutes),
        "recovery_success_rate": float(np.mean(recoveries)) if recoveries else None,
        "recovery_success_n": len(recoveries),
        "mean_post_reroute_max_utilization": (
            _mean_ci95(np.array(post_reroute_utils)) if post_reroute_utils else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episodes", type=int, default=None, help="override config.rl.eval_episodes")
    parser.add_argument("--policy-path", type=str, default=None, help="override config.rl.policy_path")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    config = load_config()
    topology_spec = load_topology_spec(config.network.topology_name)
    reward_weights = RewardWeights(**dataclasses.asdict(config.rl.reward_weights))
    env_config = EnvConfig(k_shortest=config.path.k_shortest, reward_weights=reward_weights)

    n_episodes = args.episodes or (10 if args.quick else config.rl.eval_episodes)
    base_seed = config.seeds.eval

    if args.policy_path is not None:
        policy_path = Path(args.policy_path)
    elif args.quick:
        policy_path = REPO_ROOT / "models" / "routing_policy_quick.zip"
    else:
        policy_path = REPO_ROOT / config.rl.policy_path

    env = SDNRoutingEnv(topology_spec, env_config=env_config, src=1, dst=6, seed=base_seed)
    model = MaskablePPO.load(str(policy_path))

    results = {}
    for policy_name in BASELINE_NAMES:
        episode_results = [
            run_episode(env, policy_name, model, seed=base_seed + i) for i in range(n_episodes)
        ]
        results[policy_name] = _summarize(episode_results)

    output = {
        "n_episodes": n_episodes,
        "base_seed": base_seed,
        "policy_path": str(policy_path.relative_to(REPO_ROOT)) if policy_path.is_relative_to(REPO_ROOT) else str(policy_path),
        "k_shortest": config.path.k_shortest,
        "data_source": "synthetic_simulator",
        "evaluated_at": time.time(),
        "results": results,
    }

    tables_dir = REPO_ROOT / "results" / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_quick" if args.quick else ""
    out_path = tables_dir / f"rl_evaluation{suffix}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    for name, summary in results.items():
        reward = summary["episode_reward"]
        print(
            f"{name}: reward={reward['mean']:.2f}±{reward['ci95_half_width']:.2f} "
            f"recovery_rate={summary['recovery_success_rate']} "
            f"reroutes_mean={summary['reroute_count']['mean']:.2f}"
        )
    print(f"saved to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
