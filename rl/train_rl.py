#!/usr/bin/env python3
"""Trains the MaskablePPO route-selection policy against
rl/env/network_env.py's fast NetworkSimulator-backed environment
(Phase 17). Every number this script reports traces to an artefact
written under results/ from THIS run -- results/tables/rl_training_
summary.json and results/figures/rl_learning_curve.png are built from
stable_baselines3's own Monitor episode log (results/raw/rl_training/
monitor.csv), not estimated or guessed.

Usage: python rl/train_rl.py [--timesteps N] [--quick]
  --quick runs a short (5000-step) smoke run for wiring verification,
  writing to a separate models/routing_policy_quick.zip so it never
  clobbers a real trained policy.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from stable_baselines3.common.monitor import Monitor

from common.config_loader import load_config
from network.topo_builder import load_topology_spec
from rl.env.network_env import EnvConfig, SDNRoutingEnv
from rl.env.rewards import RewardWeights


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    except Exception:
        return None


def _mask_fn(env) -> np.ndarray:
    # ActionMasker calls this with whatever wrapper sits directly under
    # it (Monitor here) -- gymnasium.Wrapper no longer forwards arbitrary
    # attribute access to the inner env, so action_masks() must be
    # resolved via .unwrapped rather than a plain attribute lookup.
    return env.unwrapped.action_masks()


def make_env(topology_spec: dict, env_config: EnvConfig, seed: int, monitor_base: Path) -> ActionMasker:
    env = SDNRoutingEnv(topology_spec, env_config=env_config, src=1, dst=6, seed=seed)
    env = Monitor(env, filename=str(monitor_base))
    return ActionMasker(env, _mask_fn)


def _read_monitor_csv(monitor_path: Path) -> list[dict]:
    """stable_baselines3's Monitor writes a `# {...}` comment header line
    followed by a standard CSV (r, l, t columns = episode reward, length,
    wall-clock time since Monitor start). Parsed by hand rather than
    pandas to keep this script's dependency footprint at exactly what
    the approved stack already includes."""
    import csv

    rows = []
    with open(monitor_path) as f:
        first_line = f.readline()
        if not first_line.startswith("#"):
            f.seek(0)
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({"r": float(row["r"]), "l": int(row["l"]), "t": float(row["t"])})
    return rows


def _monitor_csv_path(base: Path) -> Path:
    """Mirrors stable_baselines3.common.monitor.ResultsWriter's own
    filename rule: a path not already ending in "monitor.csv" gets
    ".monitor.csv" appended. `base` here never ends in that suffix, so
    the on-disk name is always predictable rather than re-derived by
    guesswork after training."""
    return Path(f"{base}.monitor.csv")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timesteps", type=int, default=None, help="override config.rl.train_steps")
    parser.add_argument("--quick", action="store_true", help="short smoke run, separate output files")
    args = parser.parse_args()

    config = load_config()
    topology_spec = load_topology_spec(config.network.topology_name)

    reward_weights = RewardWeights(**dataclasses.asdict(config.rl.reward_weights))
    env_config = EnvConfig(k_shortest=config.path.k_shortest, reward_weights=reward_weights)

    total_timesteps = args.timesteps or (5000 if args.quick else config.rl.train_steps)
    seed = config.seeds.rl_train

    raw_dir = REPO_ROOT / "results" / "raw" / "rl_training"
    raw_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_quick" if args.quick else ""
    monitor_base = raw_dir / f"rl_train{suffix}"

    env = make_env(topology_spec, env_config, seed, monitor_base)

    model = MaskablePPO(
        "MlpPolicy",
        env,
        verbose=1,
        n_steps=512,
        batch_size=64,
        seed=seed,
    )

    start = time.time()
    model.learn(total_timesteps=total_timesteps)
    wall_clock_s = time.time() - start

    models_dir = REPO_ROOT / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    policy_filename = Path(config.rl.policy_path).stem + suffix + ".zip"
    policy_path = models_dir / policy_filename
    model.save(str(policy_path))

    monitor_csv_path = _monitor_csv_path(monitor_base)
    episodes = _read_monitor_csv(monitor_csv_path)

    rewards = [e["r"] for e in episodes]
    last_n = min(20, len(rewards))
    summary = {
        "algorithm": config.rl.algorithm,
        "total_timesteps": total_timesteps,
        "seed": seed,
        "num_episodes": len(episodes),
        "wall_clock_s": wall_clock_s,
        "mean_episode_reward_all": float(np.mean(rewards)) if rewards else None,
        "mean_episode_reward_last_20": float(np.mean(rewards[-last_n:])) if rewards else None,
        "reward_weights": dataclasses.asdict(config.rl.reward_weights),
        "k_shortest": config.path.k_shortest,
        "git_commit": _git_commit(),
        "trained_at": time.time(),
        "policy_path": str(policy_path.relative_to(REPO_ROOT)),
        "monitor_csv": str(monitor_csv_path.relative_to(REPO_ROOT)),
        "data_source": "synthetic_simulator",
    }

    tables_dir = REPO_ROOT / "results" / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    summary_filename = f"rl_training_summary{suffix}.json"
    with open(tables_dir / summary_filename, "w") as f:
        json.dump(summary, f, indent=2)

    if rewards:
        figures_dir = REPO_ROOT / "results" / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(figsize=(10, 5.5))
        episode_nums = list(range(1, len(rewards) + 1))
        ax.plot(episode_nums, rewards, alpha=0.3, label="episode reward")
        if len(rewards) >= 10:
            window = min(20, len(rewards) // 5 or 1)
            kernel = np.ones(window) / window
            smoothed = np.convolve(rewards, kernel, mode="valid")
            ax.plot(
                episode_nums[window - 1 :], smoothed, linewidth=2,
                label=f"{window}-episode moving average",
            )
        ax.set_xlabel("Episode")
        ax.set_ylabel("Episode reward")
        ax.set_title(
            f"RL training learning curve ({config.rl.algorithm}, {total_timesteps} timesteps,\n"
            f"synthetic_simulator, seed={seed})",
            fontsize=11,
        )
        ax.legend()
        fig.tight_layout()
        fig.savefig(figures_dir / f"rl_learning_curve{suffix}.png", dpi=150)
        plt.close(fig)

    print(f"trained {total_timesteps} timesteps over {len(episodes)} episodes in {wall_clock_s:.1f}s")
    print(f"mean episode reward (last {last_n}): {summary['mean_episode_reward_last_20']}")
    print(f"policy saved to {policy_path}")
    print(f"summary saved to {tables_dir / summary_filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
