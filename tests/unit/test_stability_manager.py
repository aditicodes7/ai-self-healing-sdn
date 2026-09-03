from controller.stability_manager import StabilityManager

COOLDOWN_S = 30.0
ROUTE_HOLD_S = 60.0
MIN_IMPROVEMENT = 0.15


def _manager() -> StabilityManager:
    return StabilityManager(cooldown_s=COOLDOWN_S, route_hold_s=ROUTE_HOLD_S, min_improvement=MIN_IMPROVEMENT)


def test_uncertain_blocks_change_regardless_of_everything_else():
    manager = _manager()
    decision = manager.evaluate(
        "h1-h6", persistent_degradation=True, uncertain=True, persistence_reason="uncertain: ...",
        current_path_cost=10.0, best_candidate_cost=1.0, now=1000.0,
    )
    assert decision.change is False
    assert decision.reason == "uncertain: ..."


def test_not_persistent_blocks_change():
    manager = _manager()
    decision = manager.evaluate(
        "h1-h6", persistent_degradation=False, uncertain=False, persistence_reason="not persistent: ...",
        current_path_cost=10.0, best_candidate_cost=1.0, now=1000.0,
    )
    assert decision.change is False


def test_insufficient_improvement_blocks_change():
    manager = _manager()
    # 5% improvement, below the 15% minimum.
    decision = manager.evaluate(
        "h1-h6", persistent_degradation=True, uncertain=False, persistence_reason="persistent",
        current_path_cost=10.0, best_candidate_cost=9.5, now=1000.0,
    )
    assert decision.change is False
    assert "insufficient improvement" in decision.reason


def test_change_allowed_when_all_conditions_met():
    manager = _manager()
    decision = manager.evaluate(
        "h1-h6", persistent_degradation=True, uncertain=False, persistence_reason="persistent",
        current_path_cost=10.0, best_candidate_cost=5.0, now=1000.0,  # 50% improvement
    )
    assert decision.change is True
    assert "reroute" in decision.reason


def test_zero_current_cost_treated_as_zero_improvement_not_a_crash():
    manager = _manager()
    decision = manager.evaluate(
        "h1-h6", persistent_degradation=True, uncertain=False, persistence_reason="persistent",
        current_path_cost=0.0, best_candidate_cost=0.0, now=1000.0,
    )
    assert decision.change is False
    assert "insufficient improvement" in decision.reason


def test_cooldown_blocks_immediate_second_change():
    manager = _manager()
    first = manager.evaluate(
        "h1-h6", True, False, "persistent", current_path_cost=10.0, best_candidate_cost=5.0, now=1000.0,
    )
    assert first.change is True

    second = manager.evaluate(
        "h1-h6", True, False, "persistent", current_path_cost=10.0, best_candidate_cost=5.0,
        now=1000.0 + COOLDOWN_S - 1,  # still inside cooldown
    )
    assert second.change is False
    assert "cooldown" in second.reason


def test_route_hold_blocks_change_after_cooldown_but_before_hold_expires():
    manager = _manager()
    manager.evaluate("h1-h6", True, False, "persistent", 10.0, 5.0, now=1000.0)

    decision = manager.evaluate(
        "h1-h6", True, False, "persistent", current_path_cost=10.0, best_candidate_cost=5.0,
        now=1000.0 + COOLDOWN_S + 1,  # past cooldown, still inside route_hold
    )
    assert decision.change is False
    assert "route hold" in decision.reason


def test_change_allowed_again_after_route_hold_expires():
    manager = _manager()
    manager.evaluate("h1-h6", True, False, "persistent", 10.0, 5.0, now=1000.0)

    decision = manager.evaluate(
        "h1-h6", True, False, "persistent", current_path_cost=10.0, best_candidate_cost=5.0,
        now=1000.0 + ROUTE_HOLD_S + 1,
    )
    assert decision.change is True


def test_flows_tracked_independently():
    manager = _manager()
    manager.evaluate("h1-h6", True, False, "persistent", 10.0, 5.0, now=1000.0)

    # A different flow, same instant, must not be blocked by h1-h6's cooldown.
    decision = manager.evaluate(
        "h2-h5", True, False, "persistent", current_path_cost=10.0, best_candidate_cost=5.0, now=1000.0,
    )
    assert decision.change is True


# -- the Phase 12 gate: oscillation prevention (Experiment 7's mechanism) ---

def test_oscillation_prevention():
    """Simulates a flow whose degradation signal flaps in and out of
    "persistent + improving" repeatedly, exactly the scenario a naive
    reroute-on-every-qualifying-signal controller would flap on. Only the
    FIRST qualifying evaluation may change the route; every subsequent one
    within cooldown/route_hold must be refused even though persistence and
    improvement both re-qualify each time."""
    manager = _manager()
    flow_key = "h1-h6"

    # Six repeated "qualifies" evaluations, one every 5 seconds -- all
    # comfortably inside route_hold_s (60s) of each other.
    decisions = [
        manager.evaluate(
            flow_key, persistent_degradation=True, uncertain=False, persistence_reason="persistent",
            current_path_cost=10.0, best_candidate_cost=5.0, now=1000.0 + i * 5.0,
        )
        for i in range(6)
    ]

    changes = [d.change for d in decisions]
    assert changes == [True, False, False, False, False, False], (
        f"expected only the first evaluation to trigger a change, got {changes}"
    )
    # Every negative decision must be explained, not just silently False.
    for d in decisions[1:]:
        assert d.reason  # non-empty
        assert "cooldown" in d.reason or "route hold" in d.reason

    # Once route_hold_s has genuinely elapsed, a still-qualifying signal
    # is allowed to change the route again -- this is stability, not a
    # permanent lockout.
    later = manager.evaluate(
        flow_key, True, False, "persistent", current_path_cost=10.0, best_candidate_cost=5.0,
        now=1000.0 + ROUTE_HOLD_S + 5.0,
    )
    assert later.change is True
