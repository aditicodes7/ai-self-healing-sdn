"""The experiment runner's per-trial config, including E5's ablation arm."""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.runner import _write_trial_config  # noqa: E402


def test_trial_config_records_the_settings_that_produced_it(tmp_path):
    cfg = yaml.safe_load(
        _write_trial_config(tmp_path, "P2_ML_RL", tmp_path / "network.db").read_text()
    )
    assert cfg["experiment"]["system"] == "P2_ML_RL"
    assert cfg["database"]["path"] == str(tmp_path / "network.db")


def test_gate_ablation_actually_removes_all_four_gates(tmp_path):
    """E5 claims the persistence and stability layer is load-bearing.

    That can only be shown by running with it and without it. The first
    E5 batch ran neither arm: nothing implemented the ablation, so all
    five trials used the default gates and the experiment was five more
    ordinary trials. Each of the four settings below has to change, since
    leaving any one of them in place still brakes the loop and would
    understate what removing the layer does.
    """
    on_dir, off_dir = tmp_path / "on", tmp_path / "off"
    on_dir.mkdir()
    off_dir.mkdir()
    control = yaml.safe_load(
        _write_trial_config(on_dir, "P2_ML_RL", tmp_path / "a.db").read_text()
    )
    ablated = yaml.safe_load(
        _write_trial_config(off_dir, "P2_ML_RL", tmp_path / "b.db", ablate_gates=True).read_text()
    )

    assert control["persistence"]["consecutive"] > 1
    assert ablated["persistence"]["consecutive"] == 1, "must act on a single prediction"
    for knob in ("cooldown_s", "route_hold_s", "min_improvement"):
        assert control["stability"][knob] > 0, f"control should still have {knob}"
        assert ablated["stability"][knob] == 0, f"{knob} must be removed, not reduced"
