import numpy as np

from controller.path_computer import LinkMetrics
from rl.env.state import build_state, state_dim

NUM_SWITCHES = 6
K = 4


def test_state_dim_matches_build_state_output_length():
    dim = state_dim(NUM_SWITCHES, K)
    state = build_state(
        src_dpid=1,
        dst_dpid=6,
        num_switches=NUM_SWITCHES,
        candidates=[],
        k=K,
        link_metrics={},
        degradation_probs={},
        degraded_link_id=None,
        current_path=None,
        time_since_last_reroute_s=0.0,
        global_mean_utilization=0.0,
        global_max_utilization=0.0,
    )
    assert state.shape == (dim,)
    assert dim == NUM_SWITCHES * 2 + K * 8 + 2 + 1


def test_src_dst_one_hot_encoding():
    state = build_state(
        src_dpid=2,
        dst_dpid=5,
        num_switches=NUM_SWITCHES,
        candidates=[],
        k=K,
        link_metrics={},
        degradation_probs={},
        degraded_link_id=None,
        current_path=None,
        time_since_last_reroute_s=0.0,
        global_mean_utilization=0.0,
        global_max_utilization=0.0,
    )
    src_onehot = state[:NUM_SWITCHES]
    dst_onehot = state[NUM_SWITCHES : 2 * NUM_SWITCHES]
    assert src_onehot[1] == 1.0
    assert src_onehot.sum() == 1.0
    assert dst_onehot[4] == 1.0
    assert dst_onehot.sum() == 1.0


def test_padded_candidate_slot_is_all_zero():
    state = build_state(
        src_dpid=1,
        dst_dpid=6,
        num_switches=NUM_SWITCHES,
        candidates=[[1, 2, 6]],
        k=K,
        link_metrics={},
        degradation_probs={},
        degraded_link_id=None,
        current_path=None,
        time_since_last_reroute_s=0.0,
        global_mean_utilization=0.0,
        global_max_utilization=0.0,
    )
    offset = NUM_SWITCHES * 2
    second_slot = state[offset + 8 : offset + 16]
    assert np.all(second_slot == 0.0)


def test_candidate_features_reflect_link_metrics():
    link_metrics = {
        "s1-s2": LinkMetrics(utilization=0.8, latency_ms=5.0, loss_rate=0.1),
        "s2-s6": LinkMetrics(utilization=0.4, latency_ms=3.0, loss_rate=0.0),
    }
    state = build_state(
        src_dpid=1,
        dst_dpid=6,
        num_switches=NUM_SWITCHES,
        candidates=[[1, 2, 6]],
        k=K,
        link_metrics=link_metrics,
        degradation_probs={"s1-s2": 0.9},
        degraded_link_id="s1-s2",
        current_path=[1, 2, 6],
        time_since_last_reroute_s=0.0,
        global_mean_utilization=0.0,
        global_max_utilization=0.0,
    )
    offset = NUM_SWITCHES * 2
    first_slot = state[offset : offset + 8]
    max_util, mean_util, max_loss, sum_latency, hop_count, max_prob, shares_degraded, equals_current = first_slot
    assert max_util == 0.8
    assert round(float(mean_util), 4) == 0.6
    assert max_loss == 0.1
    assert sum_latency == 8.0
    assert hop_count == 2.0
    assert max_prob == 0.9
    assert shares_degraded == 1.0
    assert equals_current == 1.0


def test_time_since_last_reroute_is_normalized_and_clamped():
    state = build_state(
        src_dpid=1,
        dst_dpid=6,
        num_switches=NUM_SWITCHES,
        candidates=[],
        k=K,
        link_metrics={},
        degradation_probs={},
        degraded_link_id=None,
        current_path=None,
        time_since_last_reroute_s=600.0,
        global_mean_utilization=0.0,
        global_max_utilization=0.0,
        max_time_since_reroute_s=300.0,
    )
    assert state[-1] == 1.0


def test_global_utilization_summary_present():
    state = build_state(
        src_dpid=1,
        dst_dpid=6,
        num_switches=NUM_SWITCHES,
        candidates=[],
        k=K,
        link_metrics={},
        degradation_probs={},
        degraded_link_id=None,
        current_path=None,
        time_since_last_reroute_s=0.0,
        global_mean_utilization=0.3,
        global_max_utilization=0.7,
    )
    offset = NUM_SWITCHES * 2 + K * 8
    assert state[offset] == 0.3
    assert state[offset + 1] == 0.7
