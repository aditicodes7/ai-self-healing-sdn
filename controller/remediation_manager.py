"""Orchestrates one remediation: given a degraded link and the flow using
it, selects a replacement path (controller/path_computer.py) and installs
it (controller/flow_manager.py). This is what turns a
stability_manager.Decision(change=True) into actual OpenFlow rules.

Has no independent way to discover host locations -- FlowContext (src/dst
switch, host-facing ports, IPs) must be resolved by the caller, typically
from main_app.py's MAC-learning table. Every route decision (including
"no alternate path available") is recorded in `route_changes`, matching
the spec's "log EVERY decision including the negatives."
"""
from __future__ import annotations

import dataclasses
import time

from common.logging_setup import configure_logging
from common.schema import RouteSelector
from controller.flow_manager import FlowInstallResult, FlowManager, FlowMatch, PathHop
from controller.path_computer import LinkMetrics, rank_candidate_paths
from database.db import Database

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
    # Selector tag recorded on every route_changes row (common/schema.py's
    # RouteSelector). ML prediction (Phase 9-10) + weighted-cost heuristic
    # path selection (Phase 11) is what this module actually does today;
    # Phase 18 replaces path SELECTION with the trained RL policy and
    # should update this to P2_ML_RL at that point, not before.
    SELECTOR = RouteSelector.P1_ML_HEURISTIC

    def __init__(
        self, db: Database, flow_manager: FlowManager, k_shortest: int,
        weight_utilization: float, weight_latency: float, weight_loss: float, weight_hop_count: float,
    ):
        self.db = db
        self.flow_manager = flow_manager
        self.k_shortest = k_shortest
        self.weight_utilization = weight_utilization
        self.weight_latency = weight_latency
        self.weight_loss = weight_loss
        self.weight_hop_count = weight_hop_count

    def remediate(
        self,
        incident_id: str,
        link_id: str,
        flow_ctx: FlowContext,
        graph,
        link_metrics: dict[str, LinkMetrics],
        datapaths: dict[int, object],
        excluded_paths: list[list[int]],
    ) -> tuple[list[int] | None, list[FlowInstallResult]]:
        """excluded_paths: every path that must NOT be (re-)selected --
        the originally-failing path plus, on a Phase 14 verifier retry
        after a FAILED verification, every path already tried for this
        incident. Returns (chosen_path, flow_install_results); chosen_path
        is None if no viable alternate exists among what's left -- the
        caller (Phase 14's Verifier) must surface this as UNRESOLVED,
        never retry forever on its own."""
        ranked = rank_candidate_paths(
            graph, flow_ctx.src_dpid, flow_ctx.dst_dpid, self.k_shortest, link_metrics,
            self.weight_utilization, self.weight_latency, self.weight_loss, self.weight_hop_count,
        )
        candidates = [(p, c) for p, c in ranked if p not in excluded_paths]
        current_path = excluded_paths[0] if excluded_paths else None

        if not candidates:
            logger.warning(
                "no alternate path available",
                extra={"incident_id": incident_id, "link_id": link_id, "flow_key": flow_ctx.flow_key},
            )
            self._record_route_change(
                incident_id, flow_ctx.flow_key, current_path, None,
                "no alternate path available", changed=False,
            )
            return None, []

        chosen_path, chosen_cost = candidates[0]
        hops = self._resolve_hops(chosen_path, flow_ctx, graph)

        self._record_route_change(
            incident_id, flow_ctx.flow_key, current_path, chosen_path,
            f"selected path {chosen_path} cost={chosen_cost:.4f}", changed=True,
        )

        match = FlowMatch(ipv4_src=flow_ctx.ipv4_src, ipv4_dst=flow_ctx.ipv4_dst)
        results = self.flow_manager.install_path(incident_id, hops, datapaths, match)
        return chosen_path, results

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
        new_path: list[int] | None, reason: str, changed: bool,
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
                "selector": self.SELECTOR.value,
            },
        )
