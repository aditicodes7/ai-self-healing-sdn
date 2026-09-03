from rl.env.rewards import RewardWeights, compute_reward

WEIGHTS = RewardWeights(
    w1_throughput=1.0,
    w2_latency=1.0,
    w3_loss=1.0,
    w4_recovery_success=1.0,
    w5_max_utilization_after=1.0,
    w6_reroute_cost=1.0,
    w7_oscillation_penalty=1.0,
    w8_failed_remediation=1.0,
)


def _base_kwargs(**overrides):
    kwargs = dict(
        weights=WEIGHTS,
        delta_throughput_bps=0.0,
        delta_latency_ms=0.0,
        delta_loss_rate=0.0,
        recovery_success=False,
        max_utilization_after=0.0,
        path_changed=False,
        reverted_within_hold_window=False,
        remediation_failed=False,
    )
    kwargs.update(overrides)
    return kwargs


def test_all_zero_inputs_give_zero_reward():
    components = compute_reward(**_base_kwargs())
    assert components.total == 0.0


def test_throughput_gain_increases_reward():
    components = compute_reward(**_base_kwargs(delta_throughput_bps=2_000_000.0))
    assert components.delta_throughput_term == 2.0
    assert components.total == 2.0


def test_latency_increase_is_penalized():
    components = compute_reward(**_base_kwargs(delta_latency_ms=20.0))
    assert components.delta_latency_term == -2.0
    assert components.total == -2.0


def test_latency_decrease_is_rewarded():
    components = compute_reward(**_base_kwargs(delta_latency_ms=-20.0))
    assert components.delta_latency_term == 2.0


def test_loss_increase_is_penalized():
    components = compute_reward(**_base_kwargs(delta_loss_rate=0.1))
    assert components.delta_loss_term == -0.1


def test_recovery_success_adds_positive_term():
    components = compute_reward(**_base_kwargs(recovery_success=True))
    assert components.recovery_success_term == 1.0
    assert components.total == 1.0


def test_max_utilization_after_is_subtracted():
    components = compute_reward(**_base_kwargs(max_utilization_after=0.9))
    assert components.max_utilization_term == 0.9
    assert components.total == -0.9


def test_reroute_cost_only_applies_when_path_changed():
    unchanged = compute_reward(**_base_kwargs(path_changed=False))
    changed = compute_reward(**_base_kwargs(path_changed=True))
    assert unchanged.reroute_cost_term == 0.0
    assert changed.reroute_cost_term == 1.0
    assert changed.total == -1.0


def test_oscillation_penalty_only_applies_when_reverted():
    components = compute_reward(**_base_kwargs(reverted_within_hold_window=True))
    assert components.oscillation_term == 1.0
    assert components.total == -1.0


def test_failed_remediation_penalty():
    components = compute_reward(**_base_kwargs(remediation_failed=True))
    assert components.failed_remediation_term == 1.0
    assert components.total == -1.0


def test_zero_weight_zeroes_out_a_term_regardless_of_signal():
    zero_throughput_weight = RewardWeights(
        w1_throughput=0.0,
        w2_latency=1.0,
        w3_loss=1.0,
        w4_recovery_success=1.0,
        w5_max_utilization_after=1.0,
        w6_reroute_cost=1.0,
        w7_oscillation_penalty=1.0,
        w8_failed_remediation=1.0,
    )
    components = compute_reward(
        **_base_kwargs(weights=zero_throughput_weight, delta_throughput_bps=5_000_000.0)
    )
    assert components.delta_throughput_term == 0.0


def test_total_combines_all_terms_with_correct_signs():
    components = compute_reward(
        **_base_kwargs(
            delta_throughput_bps=1_000_000.0,
            delta_latency_ms=-10.0,
            delta_loss_rate=-0.05,
            recovery_success=True,
            max_utilization_after=0.5,
            path_changed=True,
            reverted_within_hold_window=False,
            remediation_failed=False,
        )
    )
    expected = 1.0 + 1.0 + 0.05 + 1.0 - 0.5 - 1.0 - 0.0 - 0.0
    assert components.total == expected


def test_custom_scales_change_normalization():
    components = compute_reward(
        **_base_kwargs(
            delta_throughput_bps=500_000.0,
            throughput_scale_bps=500_000.0,
        )
    )
    assert components.delta_throughput_term == 1.0
