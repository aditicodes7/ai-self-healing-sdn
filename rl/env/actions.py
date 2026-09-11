"""Discrete(K+1) action space: action 0 = "do not reroute", actions
1..K = pick candidate path i (1-indexed to leave 0 for no-op).
get_action_mask() is what MaskablePPO (sb3-contrib) uses for invalid
-action masking: action 0 is always valid; action i is valid only if a
real (non-padded) candidate exists at that slot -- never masked out then
silently allowed anyway.
"""
from __future__ import annotations

import numpy as np


def num_actions(k: int) -> int:
    return k + 1


def get_action_mask(num_candidates: int, k: int, allow_hold: bool = True) -> np.ndarray:
    """Valid actions for this state. Action 0 is "do not reroute".

    `allow_hold=False` masks action 0 out, and exists because of what it
    fixes. In live remediation the policy is consulted only AFTER the
    healing loop has already decided to act -- an incident is open and
    persistence is satisfied -- so "hold" there contradicts the decision
    that caused the call. The caller already refused to honour it
    (remediation_manager's `rl_selected_no_reroute`) and quietly used the
    weighted-cost heuristic instead. Measured in E6: that happened in 7
    of 11 selections, so the learned policy was making barely a third of
    the decisions attributed to it, and P1-vs-P2 was mostly comparing the
    heuristic against itself.

    Masking is the right mechanism rather than overriding the output
    after the fact: MaskablePPO renormalises over the allowed actions, so
    the policy picks its best REROUTE, instead of us discarding its
    answer and asking a different algorithm. Hold stays available when
    there is nothing to switch to, since a mask with no valid action is
    undefined.
    """
    mask = np.zeros(k + 1, dtype=bool)
    usable = min(num_candidates, k)
    mask[1 : 1 + usable] = True
    mask[0] = allow_hold or usable == 0
    return mask


def action_to_path(
    action: int, candidates: list[list[int]], current_path: list[int] | None
) -> list[int] | None:
    """action=0 -> stay on current_path (no change). action=i (1..K) ->
    candidates[i-1]. Caller must validate `action` against
    get_action_mask() first -- this raises on an out-of-range candidate
    index rather than silently returning something wrong."""
    if action == 0:
        return current_path
    idx = action - 1
    if idx < 0 or idx >= len(candidates):
        raise IndexError(f"action {action} selects candidate {idx}, but only {len(candidates)} exist")
    return candidates[idx]
