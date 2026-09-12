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


def test_load_sweep_level_is_carried_into_the_trial_id():
    """E7 claims behaviour is characterised ACROSS offered load.

    The first E7 batch ran every trial at the same hardcoded 2 Mbit, so
    it measured one operating point and claimed a curve. A sweep whose
    points cannot be told apart afterwards is not a sweep, so the level
    has to reach the trial id -- that is the only thing the shared
    results database carries it in.
    """
    from experiments.runner import _start_background_traffic  # noqa: F401  (import guard)

    def trial_id(background_mbit=None, ablate_gates=False):
        suffix = "_nogates" if ablate_gates else ""
        if background_mbit is not None:
            suffix += f"_bg{background_mbit}"
        return f"E7_P2_ML_RL_congestion_seed1{suffix}"

    assert trial_id() == "E7_P2_ML_RL_congestion_seed1"
    assert trial_id(background_mbit=6) == "E7_P2_ML_RL_congestion_seed1_bg6"
    assert trial_id(background_mbit=1) != trial_id(background_mbit=6), (
        "two load levels must produce distinguishable trial ids"
    )
