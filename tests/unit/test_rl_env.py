

def test_hold_can_be_masked_out_but_never_leaves_an_empty_mask():
    """allow_hold=False is what makes the policy choose a reroute instead
    of declining and handing the decision to the heuristic. It must not
    be able to produce a mask with no valid action, which is undefined
    for MaskablePPO -- so with nothing to switch to, hold survives."""
    from rl.env.actions import get_action_mask

    with_hold = get_action_mask(3, 5)
    without = get_action_mask(3, 5, allow_hold=False)
    assert with_hold[0] and not without[0]
    assert list(with_hold[1:]) == list(without[1:]), "only action 0 should differ"

    no_candidates = get_action_mask(0, 5, allow_hold=False)
    assert no_candidates[0], "hold must remain valid when there is nothing to reroute to"
    assert no_candidates.any(), "a mask with no valid action is undefined"
