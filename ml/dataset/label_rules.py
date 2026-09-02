"""Labeling logic for the degradation-prediction dataset (Phase 8/9).

Label = 1 if the link enters a degraded/failed state (a fault_start
ground-truth event, from network/failure_injector.py) WITHIN THE NEXT
label.horizon_s seconds of the window's timestamp. This gives the model
lead time -- a model that only detects an already-dead link is not the
point of this project.

Pure stdlib, no numpy/pandas needed: kept separate from
common/features.py so both the dataset generator and any later "what
would this window have been labelled" tooling have one canonical source
for the labeling rule itself, decoupled from feature computation.
"""
from __future__ import annotations


def label_window(window_ts: float, link_id: str, fault_events: list[dict], horizon_s: float) -> int:
    """1 if `link_id` has a fault_start event with
    window_ts < start_ts <= window_ts + horizon_s, else 0.

    fault_events: ground-truth records as written by
    failure_injector.py's FailureInjector._write_ground_truth (a mix of
    "fault_start"/"fault_end"/"recovery" events across possibly multiple
    links) -- this function filters for fault_start events on link_id
    itself, so callers can pass the whole run's ground truth unfiltered.
    """
    for ev in fault_events:
        if ev.get("event") != "fault_start" or ev.get("link_id") != link_id:
            continue
        start_ts = ev["start_ts"]
        if window_ts < start_ts <= window_ts + horizon_s:
            return 1
    return 0
