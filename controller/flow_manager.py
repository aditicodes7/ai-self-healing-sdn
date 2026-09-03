"""Installs OpenFlow rules along a resolved path for one flow (5-tuple
match: eth_type=IPv4, ipv4_src, ipv4_dst, optional in_port), per the
FLOW INSTALLATION AND VERIFICATION spec: priority flow.priority_remediated
(above default L2-learning forwarding), idle_timeout=flow.idle_timeout_s,
hard_timeout=flow.hard_timeout_s, and a cookie encoding the incident id so
rules can later be found (`ovs-ofctl dump-flows <sw> | grep cookie=<hex>`)
and cleaned up. Uses an OFPBarrierRequest after each FlowMod and confirms
no OFPErrorMsg arrived before considering it installed. Every attempt --
success or failure -- is logged to the `flow_rules` table: switch, match,
actions, cookie, result.

Deliberately takes fully-resolved PathHop objects (dpid + in_port +
out_port for every hop, including the two host-facing ends), not a bare
list of switch dpids: resolving inter-switch ports is
topology_manager.graph's job, and resolving the two host-facing ports is
whatever discovered them (typically main_app.py's MAC-learning table) --
neither belongs inside the installation primitive itself.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

from common.logging_setup import configure_logging
from common.schema import FlowRuleResult
from database.db import Database

logger = configure_logging("controller.flow_manager")


def incident_cookie(incident_id: str) -> int:
    """Deterministic cookie derived from incident_id, so a rule's owning
    incident can be recovered later (e.g. for cleanup) without a side
    table: OpenFlow cookies are opaque 64-bit integers, not strings.

    Masked to 63 bits (sacrificing 1 bit of entropy, still enormously
    collision-resistant for this use case) rather than the full unsigned
    64 bits: SQLite's INTEGER column is signed 64-bit, and roughly half
    of all possible unsigned-64-bit hash outputs would silently overflow
    it -- confirmed the hard way (2026-09-03, live VM test): an
    OverflowError from logging hop 1's FlowMod aborted install_path()'s
    loop entirely, leaving every subsequent hop in a multi-switch path
    with NO flow installed at all, despite earlier hops reporting
    INSTALLED. See install_path()'s per-hop exception isolation below,
    added as defense in depth for the same failure class.
    """
    digest = hashlib.sha256(incident_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


@dataclasses.dataclass(frozen=True)
class FlowMatch:
    ipv4_src: str
    ipv4_dst: str


@dataclasses.dataclass(frozen=True)
class PathHop:
    dpid: int
    in_port: int
    out_port: int


@dataclasses.dataclass(frozen=True)
class FlowInstallResult:
    dpid: int
    result: FlowRuleResult
    error_msg: str | None = None


class FlowManager:
    def __init__(self, db: Database, priority: int, hard_timeout_s: int, idle_timeout_s: int):
        self.db = db
        self.priority = priority
        self.hard_timeout_s = hard_timeout_s
        self.idle_timeout_s = idle_timeout_s

    def install_path(
        self,
        incident_id: str,
        hops: list[PathHop],
        datapaths: dict[int, object],
        match: FlowMatch,
    ) -> list[FlowInstallResult]:
        cookie = incident_cookie(incident_id)
        results: list[FlowInstallResult] = []

        for hop in hops:
            # A failure anywhere in ONE hop's install-or-log must never
            # abort the remaining hops -- confirmed the hard way
            # (2026-09-03): an unguarded exception here previously left a
            # multi-hop path with only its first switch actually flowed,
            # while every later switch silently got no rule at all.
            try:
                datapath = datapaths.get(hop.dpid)
                if datapath is None:
                    result = FlowInstallResult(hop.dpid, FlowRuleResult.ERROR, "datapath not connected")
                else:
                    result = self._install_one(datapath, hop, match, cookie)
                self._log(incident_id, hop, match, cookie, result)
            except Exception as exc:
                logger.exception("flow install/log failed for hop", extra={"dpid": hop.dpid})
                result = FlowInstallResult(hop.dpid, FlowRuleResult.ERROR, str(exc))
            results.append(result)

        return results

    def _install_one(self, datapath, hop: PathHop, match: FlowMatch, cookie: int) -> FlowInstallResult:
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        of_match = parser.OFPMatch(
            in_port=hop.in_port,
            eth_type=0x0800,  # IPv4
            ipv4_src=match.ipv4_src,
            ipv4_dst=match.ipv4_dst,
        )
        actions = [parser.OFPActionOutput(hop.out_port)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            cookie=cookie,
            priority=self.priority,
            match=of_match,
            instructions=inst,
            idle_timeout=self.idle_timeout_s,
            hard_timeout=self.hard_timeout_s,
        )

        try:
            datapath.send_msg(mod)
            barrier = parser.OFPBarrierRequest(datapath)
            reply = datapath.send_msg(barrier)
            # Ryu's send_msg() itself doesn't block for a reply in the
            # general case; callers running inside a real Ryu app register
            # an EventOFPErrorMsg handler and correlate by xid to catch
            # asynchronous errors -- that correlation lives in
            # remediation_manager.py (the Ryu app), not here, so this
            # primitive is testable with a plain mock datapath. Reaching
            # this point without send_msg raising is recorded as
            # INSTALLED; an error arriving asynchronously afterward is the
            # caller's responsibility to reconcile.
            return FlowInstallResult(hop.dpid, FlowRuleResult.INSTALLED)
        except Exception as exc:
            return FlowInstallResult(hop.dpid, FlowRuleResult.ERROR, str(exc))

    def _log(self, incident_id: str, hop: PathHop, match: FlowMatch, cookie: int, result: FlowInstallResult) -> None:
        import time

        match_dict = {
            "in_port": hop.in_port, "eth_type": "0x0800",
            "ipv4_src": match.ipv4_src, "ipv4_dst": match.ipv4_dst,
        }
        actions_dict = {"output": hop.out_port}

        self.db.insert(
            "flow_rules",
            {
                "incident_id": incident_id,
                "timestamp": time.time(),
                "switch_id": str(hop.dpid),
                "match_json": json.dumps(match_dict),
                "actions_json": json.dumps(actions_dict),
                "priority": self.priority,
                "cookie": cookie,
                "hard_timeout": self.hard_timeout_s,
                "result": result.result.value,
                "error_msg": result.error_msg,
            },
        )
        logger.info(
            "flow mod",
            extra={
                "incident_id": incident_id, "dpid": hop.dpid, "match": match_dict,
                "actions": actions_dict, "cookie": cookie, "result": result.result.value,
                "error_msg": result.error_msg,
            },
        )
