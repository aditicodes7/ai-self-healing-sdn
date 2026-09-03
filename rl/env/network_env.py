"""Gymnasium environment tying together NetworkSimulator (fast queueing
approximation), state.py (fixed-size observation), actions.py (Discrete
K+1 with masking), and rewards.py (weighted reward terms) into one
route-selection RL environment for MaskablePPO training.

Episode design: one fixed demand (h1->h6, i.e. dpid 1->6 -- this
project's flagship demand; see network/traffic_generator.py and the
topology's own docstring) carrying a background flow at a fixed offered
load. At a seeded-random step, a fault (matching
network/failure_injector.py's FAULT_TYPES) is injected on a
seeded-random link that lies on the flow's CURRENT path -- guaranteeing
the fault actually matters, since an episode where it never touches the
active flow would teach the policy nothing. From then on, each step the
agent picks among up to K=path.k_shortest candidate paths, or holds.

Predicted-degradation-probability feature: this environment does NOT
call the real trained ML classifier (service/ml_rl_service.py) inside
the simulated step loop. That model was trained on real Mininet-derived
telemetry feature distributions (ml/train_model.py); feeding it this
fast simulator's approximated per-step metrics would introduce an
unvalidated distributional mismatch outside anything Phase 16's
calibration covered. Instead, degradation_probs is a documented proxy
computed directly from the simulator's own ground-truth
utilization/loss/fault state (_proxy_degradation_prob below) -- close
enough to give the policy a meaningful, direction-correct signal to
condition on, never claimed to BE the real classifier's output. Stated
sim-to-real gap; see docs/decisions.md and docs/limitations.md.
"""
from __future__ import annotations

import dataclasses

import gymnasium as gym
import networkx as nx
import numpy as np
from gymnasium import spaces

from controller.path_computer import LinkMetrics, generate_candidate_paths, link_id_for_edge
from rl.env.actions import action_to_path, get_action_mask, num_actions
from rl.env.rewards import RewardWeights, compute_reward
from rl.env.simulator import NetworkSimulator
from rl.env.state import build_state, state_dim

FAULT_TYPES = (
    "congestion",
    "gradual_degradation",
    "packet_loss",
    "high_latency",
    "rising_errors",
    "link_failure",
)


def _sample_fault_params(rng: np.random.Generator, fault_type: str) -> dict:
    if fault_type == "congestion":
        return dict(capacity_multiplier=float(rng.uniform(0.2, 0.5)))
    if fault_type == "gradual_degradation":
        return dict(
            capacity_multiplier=float(rng.uniform(0.3, 0.6)),
            extra_delay_ms=float(rng.uniform(10, 30)),
        )
    if fault_type == "packet_loss":
        return dict(loss_rate=float(rng.uniform(0.1, 0.3)))
    if fault_type == "high_latency":
        return dict(extra_delay_ms=float(rng.uniform(20, 60)))
    if fault_type == "rising_errors":
        return dict(loss_rate=float(rng.uniform(0.05, 0.2)))
    if fault_type == "link_failure":
        return dict(down=True)
    raise ValueError(f"unknown fault_type {fault_type!r}")


def _proxy_degradation_prob(link, link_result: dict) -> float:
    """Documented stand-in for the real ML classifier's output (see
    module docstring): a deterministic function of this step's own
    utilization/loss/fault state, clipped to [0, 1]. Not a claim this IS
    a calibrated probability -- only that it moves the same direction a
    real predictor's output would (higher under real stress)."""
    fault_signal = (
        1.0
        if (
            link.fault_down
            or link.fault_capacity_multiplier < 1.0
            or link.fault_extra_delay_ms > 0
            or link.fault_loss_rate > 0
        )
        else 0.0
    )
    score = 0.5 * link_result["utilization"] + 0.3 * link_result["loss_rate"] + 0.2 * fault_signal
    return float(np.clip(score, 0.0, 1.0))


@dataclasses.dataclass(frozen=True)
class EnvConfig:
    k_shortest: int = 4
    episode_steps: int = 60
    fault_onset_step_min: int = 10
    fault_onset_step_max: int = 30
    base_offered_load_bps: float = 4_000_000.0
    route_hold_steps: int = 6
    reward_weights: RewardWeights = dataclasses.field(
        default_factory=lambda: RewardWeights(
            w1_throughput=1.0,
            w2_latency=1.0,
            w3_loss=1.0,
            w4_recovery_success=2.0,
            w5_max_utilization_after=1.0,
            w6_reroute_cost=0.5,
            w7_oscillation_penalty=1.5,
            w8_failed_remediation=2.0,
        )
    )


class SDNRoutingEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(
        self,
        topology_spec: dict,
        env_config: EnvConfig | None = None,
        src: int = 1,
        dst: int = 6,
        seed: int | None = None,
    ):
        super().__init__()
        self.topology_spec = topology_spec
        self.cfg = env_config or EnvConfig()
        self.src = src
        self.dst = dst
        self.k = self.cfg.k_shortest

        self.graph = nx.Graph()
        for link in topology_spec["links"]:
            a = int(link["endpoints"][0].lstrip("s"))
            b = int(link["endpoints"][1].lstrip("s"))
            self.graph.add_edge(a, b)
        self.num_switches = self.graph.number_of_nodes()

        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(state_dim(self.num_switches, self.k),), dtype=np.float32
        )
        self.action_space = spaces.Discrete(num_actions(self.k))

        self._np_rng = np.random.default_rng(seed)
        self.sim: NetworkSimulator | None = None
        self._reset_episode_state()

    def _reset_episode_state(self) -> None:
        self.step_count = 0
        self.current_path: list[int] = []
        self.candidates: list[list[int]] = []
        self.fault_link_id: str | None = None
        self.fault_onset_step: int | None = None
        self.fault_active = False
        self.time_since_last_reroute = 0.0
        self.reroute_history: list[tuple[int, list[int]]] = []
        self._pending_fault_link_id: str | None = None
        self._pending_fault_type: str | None = None
        self._pending_fault_params: dict = {}

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        if seed is not None:
            self._np_rng = np.random.default_rng(seed)
        self._reset_episode_state()

        self.sim = NetworkSimulator(self.topology_spec, seed=int(self._np_rng.integers(0, 2**31 - 1)))
        self.candidates = generate_candidate_paths(self.graph, self.src, self.dst, self.k)
        self.current_path = self.candidates[0] if self.candidates else [self.src, self.dst]
        self.sim.set_flow("main", self.current_path, self.cfg.base_offered_load_bps)

        self.fault_onset_step = int(
            self._np_rng.integers(self.cfg.fault_onset_step_min, self.cfg.fault_onset_step_max + 1)
        )
        fault_type = str(self._np_rng.choice(FAULT_TYPES))
        candidate_links = self._current_path_link_ids()
        self._pending_fault_link_id = str(self._np_rng.choice(candidate_links)) if candidate_links else None
        self._pending_fault_type = fault_type
        self._pending_fault_params = _sample_fault_params(self._np_rng, fault_type)

        step_result = self.sim.step()
        obs = self._build_observation(step_result)
        return obs, {}

    def _current_path_link_ids(self) -> list[str]:
        return [link_id_for_edge(u, v) for u, v in zip(self.current_path, self.current_path[1:])]

    def _link_metrics_from_step(self, step_result: dict) -> dict[str, LinkMetrics]:
        # A down link (NetworkSimulator.link_aggregate_metrics) reports
        # latency_ms=inf -- correct as a "queueing delay is undefined"
        # signal, but an inf feeding the policy's MLP produces NaN
        # logits (confirmed: a link_failure fault crashed training with
        # NaN action probabilities before this clip was added). Clipped
        # to a large finite sentinel; loss_rate=1.0 and the
        # shares_degraded_link/current-path features already tell the
        # policy this link is unusable without needing an infinite value.
        return {
            lid: LinkMetrics(
                utilization=r["utilization"],
                latency_ms=min(r["latency_ms"], 10_000.0),
                loss_rate=r["loss_rate"],
            )
            for lid, r in step_result["links"].items()
        }

    def _degradation_probs_from_step(self, step_result: dict) -> dict[str, float]:
        return {
            lid: _proxy_degradation_prob(self.sim.links[lid], r) for lid, r in step_result["links"].items()
        }

    def _build_observation(self, step_result: dict) -> np.ndarray:
        link_metrics = self._link_metrics_from_step(step_result)
        degradation_probs = self._degradation_probs_from_step(step_result)
        utils = [r["utilization"] for r in step_result["links"].values()]
        state = build_state(
            src_dpid=self.src,
            dst_dpid=self.dst,
            num_switches=self.num_switches,
            candidates=self.candidates,
            k=self.k,
            link_metrics=link_metrics,
            degradation_probs=degradation_probs,
            degraded_link_id=self.fault_link_id,
            current_path=self.current_path,
            time_since_last_reroute_s=self.time_since_last_reroute,
            global_mean_utilization=float(np.mean(utils)) if utils else 0.0,
            global_max_utilization=float(max(utils)) if utils else 0.0,
        )
        return state.astype(np.float32)

    def action_masks(self) -> np.ndarray:
        return get_action_mask(len(self.candidates), self.k)

    def _current_path_avoids_fault(self) -> bool:
        if self.fault_link_id is None:
            return True
        return self.fault_link_id not in self._current_path_link_ids()

    @staticmethod
    def _clip_flow_latency(flow: dict) -> dict:
        # Same inf-latency issue as _link_metrics_from_step, but for a
        # whole path's summed latency when one of its hops is down --
        # clipped so delta_latency_ms in the reward is never inf-inf=nan.
        return {**flow, "latency_ms": min(flow["latency_ms"], 10_000.0)}

    def step(self, action: int):
        if self.step_count == self.fault_onset_step and self._pending_fault_link_id is not None:
            self.sim.inject_fault(self._pending_fault_link_id, **self._pending_fault_params)
            self.fault_link_id = self._pending_fault_link_id
            self.fault_active = True

        before = self.sim.step()
        before_flow = self._clip_flow_latency(before["flows"]["main"])

        mask = self.action_masks()
        if not mask[action]:
            # A masked-invalid action should never reach here under
            # MaskablePPO; this fallback keeps step() safe for any caller
            # (e.g. random-baseline evaluation) by treating it as a no-op
            # rather than silently rerouting to something nonexistent.
            action = 0

        new_path = action_to_path(action, self.candidates, self.current_path)
        path_changed = new_path != self.current_path

        reverted_within_hold_window = False
        if path_changed:
            for hist_step, hist_path in reversed(self.reroute_history):
                if self.step_count - hist_step > self.cfg.route_hold_steps:
                    break
                if hist_path == new_path:
                    reverted_within_hold_window = True
                    break
            self.reroute_history.append((self.step_count, new_path))
            self.sim.set_flow("main", new_path, self.cfg.base_offered_load_bps)
            self.current_path = new_path
            self.time_since_last_reroute = 0.0
        else:
            self.time_since_last_reroute += 1.0

        after = self.sim.step()
        after_flow = self._clip_flow_latency(after["flows"]["main"])

        remediation_failed = path_changed and after_flow["throughput_bps"] <= 0.0
        recovery_success = (
            self.fault_active
            and self._current_path_avoids_fault()
            and after_flow["loss_rate"] < 0.05
            and after_flow["throughput_bps"] > 0.0
        )

        utils_after = [r["utilization"] for r in after["links"].values()]
        max_utilization_after = max(utils_after) if utils_after else 0.0

        components = compute_reward(
            weights=self.cfg.reward_weights,
            delta_throughput_bps=after_flow["throughput_bps"] - before_flow["throughput_bps"],
            delta_latency_ms=after_flow["latency_ms"] - before_flow["latency_ms"],
            delta_loss_rate=after_flow["loss_rate"] - before_flow["loss_rate"],
            recovery_success=recovery_success,
            max_utilization_after=max_utilization_after,
            path_changed=path_changed,
            reverted_within_hold_window=reverted_within_hold_window,
            remediation_failed=remediation_failed,
        )

        self.step_count += 1
        terminated = False
        truncated = self.step_count >= self.cfg.episode_steps
        obs = self._build_observation(after)
        info = {
            "path_changed": path_changed,
            "recovery_success": recovery_success,
            "remediation_failed": remediation_failed,
            "fault_active": self.fault_active,
            "reward_components": dataclasses.asdict(components),
        }
        return obs, components.total, terminated, truncated, info
