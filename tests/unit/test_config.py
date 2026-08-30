import dataclasses
from pathlib import Path

import pytest

from common.config_loader import Config, ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"


def test_default_config_loads():
    config = load_config()
    assert isinstance(config, Config)


def test_explicit_path_loads():
    config = load_config(CONFIG_PATH)
    assert config.project.name == "ai-self-healing-sdn"


def test_missing_file_raises():
    with pytest.raises(ConfigError):
        load_config(REPO_ROOT / "config" / "does_not_exist.yaml")


def test_frozen_dataclasses_are_immutable():
    config = load_config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.telemetry.interval_s = 99


def test_telemetry_values():
    config = load_config()
    assert config.telemetry.interval_s == 2
    assert config.telemetry.probe_timeout_s == 1.0


def test_persistence_uncertainty_band_ordered_and_typed():
    config = load_config()
    low, high = config.persistence.uncertainty_band
    assert isinstance(low, float) and isinstance(high, float)
    assert low < high
    assert low == 0.45 and high == 0.60


def test_persistence_window_and_consecutive():
    config = load_config()
    assert config.persistence.window == 4
    assert config.persistence.consecutive == 3
    assert config.persistence.consecutive <= config.persistence.window


def test_stability_defaults():
    config = load_config()
    assert config.stability.cooldown_s == 30
    assert config.stability.route_hold_s == 60
    assert 0 < config.stability.min_improvement < 1


def test_ml_threshold_within_bounds():
    config = load_config()
    assert 0 < config.ml.threshold < 1
    low, high = config.persistence.uncertainty_band
    assert not (low < config.ml.threshold < high), (
        "ml.threshold must sit outside the uncertainty band, otherwise "
        "the abstention logic can never fire at the configured threshold"
    )


def test_rl_reward_weights_present():
    config = load_config()
    weights = config.rl.reward_weights
    for field in dataclasses.fields(weights):
        value = getattr(weights, field.name)
        assert isinstance(value, (int, float))


def test_path_k_shortest_positive():
    config = load_config()
    assert config.path.k_shortest >= 1


def test_verify_tolerance_fractions():
    config = load_config()
    assert 0 < config.verify.tolerance.latency_pct < 1
    assert 0 < config.verify.tolerance.loss_pct < 1


def test_seeds_dataset_runs_at_least_eight():
    config = load_config()
    assert len(config.seeds.dataset_runs) >= 8


def test_malformed_yaml_missing_section_raises(tmp_path):
    bad = tmp_path / "bad_config.yaml"
    bad.write_text("project:\n  name: x\n")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_malformed_uncertainty_band_raises(tmp_path):
    good = CONFIG_PATH.read_text()
    bad = good.replace(
        "uncertainty_band: [0.45, 0.60]", "uncertainty_band: [0.60, 0.45]"
    )
    bad_path = tmp_path / "bad_config.yaml"
    bad_path.write_text(bad)
    with pytest.raises(ConfigError):
        load_config(bad_path)
