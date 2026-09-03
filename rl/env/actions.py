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


def get_action_mask(num_candidates: int, k: int) -> np.ndarray:
    mask = np.zeros(k + 1, dtype=bool)
    mask[0] = True  # "do not reroute" is always valid, even with zero candidates
    mask[1 : 1 + min(num_candidates, k)] = True
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
