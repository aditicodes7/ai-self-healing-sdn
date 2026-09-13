"""Loads config/config.yaml into frozen dataclasses.

The dataclasses are the single source of truth for what a valid config looks
like: a malformed or incomplete YAML file fails fast at load time instead of
surfacing as a KeyError deep inside a controller or training script.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "config.yaml"


@dataclasses.dataclass(frozen=True)
class ProjectConfig:
    name: str
    controller_backend: str | None


@dataclasses.dataclass(frozen=True)
class ControllerConfig:
    openflow_version: str
    observe_links: bool
    of_listen_host: str
    of_listen_port: int


@dataclasses.dataclass(frozen=True)
class NetworkConfig:
    topology_name: str


@dataclasses.dataclass(frozen=True)
class TelemetryConfig:
    interval_s: float
    probe_timeout_s: float
    echo_interval_s: float


@dataclasses.dataclass(frozen=True)
class FeaturesConfig:
    windows: tuple[int, ...]
    ewma_alpha: float
    baseline_healthy_windows: int
    min_history_for_window: bool


@dataclasses.dataclass(frozen=True)
class LabelConfig:
    horizon_s: float


@dataclasses.dataclass(frozen=True)
class MLConfig:
    threshold: float
    model_path: str
    metadata_path: str
    candidate_models: tuple[str, ...]
    inference_timeout_ms: int


@dataclasses.dataclass(frozen=True)
class MLRLServiceConfig:
    host: str
    port: int


@dataclasses.dataclass(frozen=True)
class PersistenceConfig:
    window: int
    consecutive: int
    uncertainty_band: tuple[float, float]


@dataclasses.dataclass(frozen=True)
class StabilityConfig:
    cooldown_s: float
    route_hold_s: float
    min_improvement: float


@dataclasses.dataclass(frozen=True)
class PathWeightsConfig:
    utilization: float
    latency: float
    loss: float
    hop_count: float


@dataclasses.dataclass(frozen=True)
class PathConfig:
    k_shortest: int
    weights: PathWeightsConfig


@dataclasses.dataclass(frozen=True)
class ExperimentConfig:
    system: str


@dataclasses.dataclass(frozen=True)
class BaselinesConfig:
    b2_utilization_threshold: float
    b2_loss_threshold: float


@dataclasses.dataclass(frozen=True)
class RLRewardWeightsConfig:
    w1_throughput: float
    w2_latency: float
    w3_loss: float
    w4_recovery_success: float
    w5_max_utilization_after: float
    w6_reroute_cost: float
    w7_oscillation_penalty: float
    w8_failed_remediation: float


@dataclasses.dataclass(frozen=True)
class RLConfig:
    algorithm: str
    timeout_ms: int
    train_steps: int
    eval_episodes: int
    policy_path: str
    reward_weights: RLRewardWeightsConfig


@dataclasses.dataclass(frozen=True)
class FlowConfig:
    priority_remediated: int
    priority_default: int
    hard_timeout_s: int
    idle_timeout_s: int


@dataclasses.dataclass(frozen=True)
class VerifyToleranceConfig:
    latency_pct: float
    loss_pct: float


@dataclasses.dataclass(frozen=True)
class VerifyConfig:
    settle_s: float
    windows: int
    tolerance: VerifyToleranceConfig


@dataclasses.dataclass(frozen=True)
class RemediationConfig:
    max_retries: int


@dataclasses.dataclass(frozen=True)
class TrafficConfig:
    iperf3_port: int
    iperf3_protocol: str


@dataclasses.dataclass(frozen=True)
class DatabaseConfig:
    path: str
    wal: bool
    retention_days: int


@dataclasses.dataclass(frozen=True)
class APIConfig:
    host: str
    port: int


@dataclasses.dataclass(frozen=True)
class SeedsConfig:
    global_: int
    dataset_runs: tuple[int, ...]
    rl_train: int
    eval: int


@dataclasses.dataclass(frozen=True)
class Config:
    project: ProjectConfig
    controller: ControllerConfig
    network: NetworkConfig
    telemetry: TelemetryConfig
    features: FeaturesConfig
    label: LabelConfig
    ml: MLConfig
    ml_rl_service: MLRLServiceConfig
    persistence: PersistenceConfig
    stability: StabilityConfig
    path: PathConfig
    baselines: BaselinesConfig
    experiment: ExperimentConfig
    rl: RLConfig
    flow: FlowConfig
    verify: VerifyConfig
    remediation: RemediationConfig
    traffic: TrafficConfig
    database: DatabaseConfig
    api: APIConfig
    seeds: SeedsConfig


class ConfigError(ValueError):
    """Raised when config/config.yaml is missing keys or has the wrong shape."""


def _get(d: dict, key: str, section: str) -> Any:
    if key not in d:
        raise ConfigError(f"config section '{section}' is missing required key '{key}'")
    return d[key]


def resolve_venv_ctrl() -> Path:
    """Where the Ryu controller's Python 3.9 environment lives.

    Both the experiment runner and the live demo start the controller as
    root (Mininet needs it), and under sudo `Path.home()` is /root -- not
    the account the environment was actually created under. The runner
    only worked because its launcher script exported VENV_CTRL; running
    the demo directly, as its own docstring tells you to, failed with
    FileNotFoundError on /root/venv-ctrl-.../bin/python3.9.

    Resolution order: an explicit VENV_CTRL, then the home of the user
    who invoked sudo, then the current user's home.
    """
    explicit = os.environ.get("VENV_CTRL")
    if explicit:
        return Path(explicit)
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        home = Path(os.path.expanduser(f"~{sudo_user}"))
        candidate = home / "venv-ctrl-ai-self-healing-sdn"
        if candidate.exists():
            return candidate
    return Path.home() / "venv-ctrl-ai-self-healing-sdn"


def load_config(path: str | Path | None = None) -> Config:
    """Resolution order: explicit `path`, then the AI_SDN_CONFIG
    environment variable, then config/config.yaml.

    AI_SDN_CONFIG exists so an out-of-process component -- notably the
    Ryu controller, which is launched as a subprocess and calls
    load_config() with no arguments -- can be pointed at a variant
    config. Phase 22's experiment harness uses it to run the same
    controller as B1/B2/P1/P2 without editing the tracked config file,
    which keeps each trial's exact settings reproducible from its own
    generated file.
    """
    if path is None:
        env_path = os.environ.get("AI_SDN_CONFIG")
        if env_path:
            path = env_path
    cfg_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"config file not found: {cfg_path}")

    with open(cfg_path, "r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError(f"config file did not parse to a mapping: {cfg_path}")

    try:
        project = _get(raw, "project", "root")
        controller = _get(raw, "controller", "root")
        network = _get(raw, "network", "root")
        telemetry = _get(raw, "telemetry", "root")
        features = _get(raw, "features", "root")
        label = _get(raw, "label", "root")
        ml = _get(raw, "ml", "root")
        ml_rl_service = _get(raw, "ml_rl_service", "root")
        persistence = _get(raw, "persistence", "root")
        stability = _get(raw, "stability", "root")
        path_cfg = _get(raw, "path", "root")
        baselines = _get(raw, "baselines", "root")
        experiment = _get(raw, "experiment", "root")
        rl = _get(raw, "rl", "root")
        flow = _get(raw, "flow", "root")
        verify = _get(raw, "verify", "root")
        remediation = _get(raw, "remediation", "root")
        traffic = _get(raw, "traffic", "root")
        database = _get(raw, "database", "root")
        api = _get(raw, "api", "root")
        seeds = _get(raw, "seeds", "root")

        uncertainty_band = tuple(_get(persistence, "uncertainty_band", "persistence"))
        if len(uncertainty_band) != 2 or uncertainty_band[0] >= uncertainty_band[1]:
            raise ConfigError(
                "persistence.uncertainty_band must be [low, high] with low < high, "
                f"got {uncertainty_band}"
            )

        config = Config(
            project=ProjectConfig(
                name=_get(project, "name", "project"),
                controller_backend=project.get("controller_backend"),
            ),
            controller=ControllerConfig(
                openflow_version=_get(controller, "openflow_version", "controller"),
                observe_links=_get(controller, "observe_links", "controller"),
                of_listen_host=_get(controller, "of_listen_host", "controller"),
                of_listen_port=_get(controller, "of_listen_port", "controller"),
            ),
            network=NetworkConfig(
                topology_name=_get(network, "topology_name", "network"),
            ),
            telemetry=TelemetryConfig(
                interval_s=_get(telemetry, "interval_s", "telemetry"),
                probe_timeout_s=_get(telemetry, "probe_timeout_s", "telemetry"),
                echo_interval_s=_get(telemetry, "echo_interval_s", "telemetry"),
            ),
            features=FeaturesConfig(
                windows=tuple(_get(features, "windows", "features")),
                ewma_alpha=_get(features, "ewma_alpha", "features"),
                baseline_healthy_windows=_get(features, "baseline_healthy_windows", "features"),
                min_history_for_window=_get(features, "min_history_for_window", "features"),
            ),
            label=LabelConfig(horizon_s=_get(label, "horizon_s", "label")),
            ml=MLConfig(
                threshold=_get(ml, "threshold", "ml"),
                model_path=_get(ml, "model_path", "ml"),
                metadata_path=_get(ml, "metadata_path", "ml"),
                candidate_models=tuple(_get(ml, "candidate_models", "ml")),
                inference_timeout_ms=_get(ml, "inference_timeout_ms", "ml"),
            ),
            ml_rl_service=MLRLServiceConfig(
                host=_get(ml_rl_service, "host", "ml_rl_service"),
                port=_get(ml_rl_service, "port", "ml_rl_service"),
            ),
            persistence=PersistenceConfig(
                window=_get(persistence, "window", "persistence"),
                consecutive=_get(persistence, "consecutive", "persistence"),
                uncertainty_band=(float(uncertainty_band[0]), float(uncertainty_band[1])),
            ),
            stability=StabilityConfig(
                cooldown_s=_get(stability, "cooldown_s", "stability"),
                route_hold_s=_get(stability, "route_hold_s", "stability"),
                min_improvement=_get(stability, "min_improvement", "stability"),
            ),
            path=PathConfig(
                k_shortest=_get(path_cfg, "k_shortest", "path"),
                weights=PathWeightsConfig(**_get(path_cfg, "weights", "path")),
            ),
            experiment=ExperimentConfig(
                system=_get(experiment, "system", "experiment"),
            ),
            baselines=BaselinesConfig(
                b2_utilization_threshold=_get(baselines, "b2_utilization_threshold", "baselines"),
                b2_loss_threshold=_get(baselines, "b2_loss_threshold", "baselines"),
            ),
            rl=RLConfig(
                algorithm=_get(rl, "algorithm", "rl"),
                timeout_ms=_get(rl, "timeout_ms", "rl"),
                train_steps=_get(rl, "train_steps", "rl"),
                eval_episodes=_get(rl, "eval_episodes", "rl"),
                policy_path=_get(rl, "policy_path", "rl"),
                reward_weights=RLRewardWeightsConfig(**_get(rl, "reward_weights", "rl")),
            ),
            flow=FlowConfig(
                priority_remediated=_get(flow, "priority_remediated", "flow"),
                priority_default=_get(flow, "priority_default", "flow"),
                hard_timeout_s=_get(flow, "hard_timeout_s", "flow"),
                idle_timeout_s=_get(flow, "idle_timeout_s", "flow"),
            ),
            verify=VerifyConfig(
                settle_s=_get(verify, "settle_s", "verify"),
                windows=_get(verify, "windows", "verify"),
                tolerance=VerifyToleranceConfig(**_get(verify, "tolerance", "verify")),
            ),
            remediation=RemediationConfig(
                max_retries=_get(remediation, "max_retries", "remediation"),
            ),
            traffic=TrafficConfig(
                iperf3_port=_get(traffic, "iperf3_port", "traffic"),
                iperf3_protocol=_get(traffic, "iperf3_protocol", "traffic"),
            ),
            database=DatabaseConfig(
                path=_get(database, "path", "database"),
                wal=_get(database, "wal", "database"),
                retention_days=_get(database, "retention_days", "database"),
            ),
            api=APIConfig(
                host=_get(api, "host", "api"),
                port=_get(api, "port", "api"),
            ),
            seeds=SeedsConfig(
                global_=_get(seeds, "global", "seeds"),
                dataset_runs=tuple(_get(seeds, "dataset_runs", "seeds")),
                rl_train=_get(seeds, "rl_train", "seeds"),
                eval=_get(seeds, "eval", "seeds"),
            ),
        )
    except ConfigError:
        raise
    except (TypeError, KeyError) as exc:
        raise ConfigError(f"malformed config structure: {exc}") from exc

    return config
