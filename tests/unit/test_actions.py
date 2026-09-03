import numpy as np
import pytest

from rl.env.actions import action_to_path, get_action_mask, num_actions


def test_num_actions_is_k_plus_one():
    assert num_actions(4) == 5


def test_action_mask_no_op_always_valid():
    mask = get_action_mask(num_candidates=0, k=4)
    assert mask[0] == True  # noqa: E712
    assert mask.sum() == 1


def test_action_mask_full_candidates():
    mask = get_action_mask(num_candidates=4, k=4)
    assert np.array_equal(mask, np.array([True] * 5))


def test_action_mask_partial_candidates_pads_false():
    mask = get_action_mask(num_candidates=2, k=4)
    assert np.array_equal(mask, np.array([True, True, True, False, False]))


def test_action_mask_never_exceeds_k_even_with_more_candidates():
    mask = get_action_mask(num_candidates=10, k=4)
    assert mask.shape == (5,)
    assert mask.sum() == 5


def test_action_zero_returns_current_path_unchanged():
    current = [1, 2, 3]
    result = action_to_path(0, candidates=[[1, 4, 3], [1, 5, 3]], current_path=current)
    assert result == current


def test_action_zero_with_no_current_path_returns_none():
    result = action_to_path(0, candidates=[[1, 4, 3]], current_path=None)
    assert result is None


def test_action_one_selects_first_candidate():
    candidates = [[1, 4, 3], [1, 5, 3]]
    result = action_to_path(1, candidates=candidates, current_path=[1, 2, 3])
    assert result == [1, 4, 3]


def test_action_two_selects_second_candidate():
    candidates = [[1, 4, 3], [1, 5, 3]]
    result = action_to_path(2, candidates=candidates, current_path=[1, 2, 3])
    assert result == [1, 5, 3]


def test_out_of_range_action_raises_instead_of_guessing():
    candidates = [[1, 4, 3]]
    with pytest.raises(IndexError):
        action_to_path(2, candidates=candidates, current_path=[1, 2, 3])
