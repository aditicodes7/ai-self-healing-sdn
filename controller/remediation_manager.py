"""Orchestrates one remediation: given a degraded link and the flow using
it, selects a replacement path (controller/path_computer.py, or the
trained RL policy via controller/rl_client.py) and installs it
(controller/flow_manager.py). This is what turns a
stability_manager.Decision(change=True) into actual OpenFlow rules.

Has no independent way to discover host locations -- FlowContext (src/dst
switch, host-facing ports, IPs) must be resolved by the caller, typically
from main_app.py's MAC-learning table. Every route decision (including
"no alternate path available") is recorded in `route_changes`, matching
the spec's "log EVERY decision including the negatives."

Phase 18 route selection: if `rl_client` is configured, RL selection is
tried first; the weighted-cost heuristic (rank_candidate_paths) is
ALWAYS computed regardless, so it's ready as an immediate fallback --
never a second network round-trip after the fact. RL is used only when
it returns a usable, non-excluded, non-hold path; every other outcome
(service down, timeout, malformed response, RL selecting "hold" when a
change was already mandated, RL selecting an already-excluded or
otherwise invalid path) falls back to the heuristic, with `rl_fallback`
and `rl_fallback_reason` recorded on the route_changes row so this is
queryable, not just log-visible.
"""
from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING

from common.logging_setup import configure_logging
from common.schema import RouteSelector
from controller.flow_manager import FlowInstallResult, FlowManager, FlowMatch, PathHop
from controller.path_computer import (
    LinkMetrics,
    generate_candidate_paths,
    link_id_for_edge,
    rank_candidate_paths,
)
from database.db import Database

if TYPE_CHECKING:
    # controller/rl_client.py imports `requests`, which venv-ml (where
    # this module's own unit tests run) deliberately does not install --
    # see requirements-ml.txt/requirements-ctrl.txt. `from __future__
    # import annotations` above already makes every type hint in this
    # file lazy, so this import is genuinely type-checker-only and must
    # never be hoisted out of the TYPE_CHECKING guard.
    from controller.rl_client import RLClient

logger = configure_logging("controller.remediation_manager")


@dataclasses.dataclass(frozen=True)
class FlowContext:
    flow_key: str
    src_dpid: int
    dst_dpid: int
    src_host_port: int  # ingress port on src_dpid facing the source host
    dst_host_port: int  # egress port on dst_dpid facing the destination host
    ipv4_src: str
    ipv4_dst: str


class RemediationManager:
    def __init__(
        self, db: Database, flow_manager: FlowManager, k_shortest: int,
        weight_utilization: float, weight_latency: float, weight_loss: float, weight_hop_count: float,
        rl_client: RLClient | None = None,
    ):
        self.db = db
        self.flow_manager = flow_manager
        self.k_shortest = k_shortest
        self.weight_utilization = weight_utilization
        self.weight_latency = weight_latency
        self.weight_loss = weight_loss
        self.weight_hop_count = weight_hop_count
        self.rl_client = rl_client

    def remediate(
        self,
        incident_id: str,
        link_id: str,
        flow_ctx: FlowContext,
        graph,
        link_metrics: dict[str, LinkMetrics],
        datapaths: dict[int, object],
        excluded_paths: list[list[int]],
        degradation_probs: dict[str, float] | None = None,
        time_since_last_reroute_s: float = 0.0,
        global_mean_utilization: float = 0.0,
        global_max_utilization: float = 0.0,
    ) -> tuple[list[int] | None, list[FlowInstallResult]]:
        """excluded_paths: every path that must NOT be (re-)selected --
        the originally-failing path plus, on a Phase 14 verifier retry
        after a FAILED verification, every path already tried for this
        incident. Returns (chosen_path, flow_install_results); chosen_path
        is None if no viable alternate exists among what's left -- the
        caller (Phase 14's Verifier) must surface this as UNRESOLVED,
        never retry forever on its own.

        degradation_probs/time_since_last_reroute_s/global_*_utilization
        are only used to build the RL policy's state when `rl_client` is
        configured; heuristic-only callers (and every existing test) can
        omit them."""
        ranked = rank_candidate_paths(
            graph, flow_ctx.src_dpid, flow_ctx.dst_dpid, self.k_shortest, link_metrics,
            self.weight_utilization, self.weight_latency, self.weight_loss, self.weight_hop_count,
        )
        heuristic_candidates = [(p, c) for p, c in ranked if p not in excluded_paths]
        current_path = excluded_paths[0] if excluded_paths else None

        chosen_path, decision_reason, selector = None, None, RouteSelector.P1_ML_HEURISTIC
        rl_fallback, rl_fallback_reason = False, None

        if self.rl_client is not None:
            chosen_path, decision_reason, rl_fallback_reason = self._try_rl_selection(
                flow_ctx, graph, link_metrics, degradation_probs or {}, degraded_link_id=link_id,
                current_path=current_path, excluded_paths=excluded_paths,
                time_since_last_reroute_s=time_since_last_reroute_s,
                global_mean_utilization=global_mean_utilization,
                global_max_utilization=global_max_utilization,
            )
            if chosen_path is not None:
                selector = RouteSelector.P2_ML_RL
            else:
                rl_fallback = True

        if chosen_path is None:
            if not heuristic_candidates:
                logger.warning(
                    "no alternate path available",
                    extra={"incident_id": incident_id, "link_id": link_id, "flow_key": flow_ctx.flow_key},
                )
                self._record_route_change(
                    incident_id, flow_ctx.flow_key, current_path, None,
                    "no alternate path available", changed=False, selector=selector,
                    rl_fallback=rl_fallback, rl_fallback_reason=rl_fallback_reason,
                )
                return None, []

            chosen_path, chosen_cost = heuristic_candidates[0]
            decision_reason = f"selected path {chosen_path} cost={chosen_cost:.4f}"
            selector = RouteSelector.P1_ML_HEURISTIC

        hops = self._resolve_hops(chosen_path, flow_ctx, graph)

        self._record_route_change(
            incident_id, flow_ctx.flow_key, current_path, chosen_path,
            decision_reason, changed=True, selector=selector,
            rl_fallback=rl_fallback, rl_fallback_reason=rl_fallback_reason,
        )

        match = FlowMatch(ipv4_src=flow_ctx.ipv4_src, ipv4_dst=flow_ctx.ipv4_dst)
        results = self.flow_manager.install_path(incident_id, hops, datapaths, match)
        return chosen_path, results

    @staticmethod
    def _path_link_ids(path: list[int]) -> list[str]:
        return [link_id_for_edge(u, v) for u, v in zip(path, path[1:])]

    def _try_rl_selection(
        self, flow_ctx, graph, link_metrics, degradation_probs, degraded_link_id,
        current_path, excluded_paths, time_since_last_reroute_s,
        global_mean_utilization, global_max_utilization,
    ) -> tuple[list[int] | None, str | None, str | None]:
        """Returns (chosen_path, decision_reason, fallback_reason).
        chosen_path is None whenever the caller must fall back to the
        heuristic -- fallback_reason then explains why, for the
        route_changes row."""
        all_candidates = generate_candidate_paths(
            graph, flow_ctx.src_dpid, flow_ctx.dst_dpid, self.k_shortest
        )
        # Only offer the policy paths it is actually allowed to pick.
        #
        # Previously the full k-shortest list was sent, including the
        # failing path and anything else crossing the degrading link, and
        # the policy's answer was then rejected if it landed on one --
        # observed live on a link_failure, where the policy chose an
        # excluded path and the safety gate fell back to the heuristic
        # (rl_selected_excluded_path). Asking for a choice and discarding
        # it wastes the policy entirely; offering only viable options
        # lets it decide among alternatives that can actually be
        # installed. The gate below still stands as defence in depth.
        raw_candidates = [
            p for p in all_candidates
            if p not in excluded_paths
            and (not degraded_link_id or degraded_link_id not in self._path_link_ids(p))
        ]
        if not raw_candidates:
            return None, None, "no_viable_candidate_for_rl"

        # allow_hold=False: this call only happens once the healing loop
        # has opened an incident and persistence is satisfied, so "do not
        # reroute" contradicts the decision that triggered it. Previously
        # the policy could answer 0 here and we would discard the answer
        # and use the heuristic -- 7 of 11 times in E6 -- which meant the
        # learned policy was credited with decisions the formula made.
        decision = self.rl_client.select_route(
            flow_ctx.src_dpid, flow_ctx.dst_dpid, raw_candidates, link_metrics,
            degradation_probs, degraded_link_id, current_path,
            time_since_last_reroute_s, global_mean_utilization, global_max_utilization,
            allow_hold=False,
        )
        if decision is None:
            return None, None, "rl_service_unavailable"
        if decision.action == 0:
            return None, None, "rl_selected_no_reroute"
        if decision.chosen_path is None or decision.chosen_path in excluded_paths:
            return None, None, "rl_selected_excluded_path"
        if decision.chosen_path not in raw_candidates:
            return None, None, "rl_selected_invalid_path"

        reason = f"RL selected path {decision.chosen_path} (action={decision.action}, policy={decision.policy_version})"
        return decision.chosen_path, reason, None

    def _resolve_hops(self, path: list[int], flow_ctx: FlowContext, graph) -> list[PathHop]:
        hops = []
        for i, dpid in enumerate(path):
            if i == 0:
                in_port = flow_ctx.src_host_port
            else:
                in_port = graph[path[i - 1]][dpid]["ports"][dpid]

            if i == len(path) - 1:
                out_port = flow_ctx.dst_host_port
            else:
                out_port = graph[dpid][path[i + 1]]["ports"][dpid]

            hops.append(PathHop(dpid=dpid, in_port=in_port, out_port=out_port))
        return hops

    def _record_route_change(
        self, incident_id: str, flow_key: str, old_path: list[int] | None,
        new_path: list[int] | None, reason: str, changed: bool, selector: RouteSelector,
        rl_fallback: bool = False, rl_fallback_reason: str | None = None,
    ) -> None:
        self.db.insert(
            "route_changes",
            {
                "incident_id": incident_id,
                "timestamp": time.time(),
                "flow_key": flow_key,
                "old_path": ",".join(f"s{d}" for d in old_path) if old_path else None,
                "new_path": ",".join(f"s{d}" for d in new_path) if new_path else None,
                "decision_reason": reason,
                "changed": 1 if changed else 0,
                "selector": selector.value,
                "rl_fallback": 1 if rl_fallback else 0,
                "rl_fallback_reason": rl_fallback_reason,
            },
        )
