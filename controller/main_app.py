"""Phase 4 controller: MAC-learning L2 forwarding, loop-safe on the
mesh topologies in network/topologies/*.yaml via TopologyManager's
spanning tree (see topology_manager.py for why flooding can't just go out
every port). Real shortest-path / RL-selected forwarding replaces flooding
entirely from Phase 11 onward; this phase only needs "hosts ping" and "the
discovered graph matches the YAML".

Run with (from repo root, so `controller.*` resolves):
  $VENV_CTRL/bin/python3.9 -m ryu.cmd.manager --observe-links controller.main_app
--observe-links is required: it's what makes ryu.topology emit the
LLDP-based link-discovery events TopologyManager listens for.
"""
from __future__ import annotations

from common.logging_setup import configure_logging
from controller.of_compat import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    app_manager,
    ether_types,
    ethernet,
    ofp_event,
    ofproto_v1_3,
    packet,
    set_ev_cls,
)
from controller.topology_manager import TopologyManager

logger = configure_logging("controller.main_app")


class L2Forwarding(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"topology_manager": TopologyManager}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.topology_manager: TopologyManager = kwargs["topology_manager"]
        # {dpid: {mac: port}}, per-switch MAC learning table.
        self.mac_to_port: dict[int, dict[str, int]] = {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Table-miss flow: anything not matching a learned entry goes to
        # the controller.
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, 0, match, actions)
        logger.info("switch connected", extra={"dpid": datapath.id})

    def _add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id is not None:
            mod = parser.OFPFlowMod(
                datapath=datapath, buffer_id=buffer_id, priority=priority,
                match=match, instructions=inst,
            )
        else:
            mod = parser.OFPFlowMod(
                datapath=datapath, priority=priority, match=match, instructions=inst,
            )
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            # Consumed by ryu.topology's own link-discovery app; never
            # learn from or forward LLDP.
            return

        dst = eth.dst
        src = eth.src

        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
            actions = [parser.OFPActionOutput(out_port)]
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            buffer_id = msg.buffer_id if msg.buffer_id != ofproto.OFP_NO_BUFFER else None
            self._add_flow(datapath, 1, match, actions, buffer_id=buffer_id)
        else:
            actions = [
                parser.OFPActionOutput(p) for p in self._flood_ports(dpid, in_port)
            ]

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port,
            actions=actions, data=data,
        )
        datapath.send_msg(out)

    def _flood_ports(self, dpid: int, in_port: int) -> list[int]:
        """Every port on `dpid` except in_port and any inter-switch port
        that isn't on the discovered spanning tree (loop prevention)."""
        all_ports = self.topology_manager.get_all_ports(dpid)
        tree_inter_switch_ports = self.topology_manager.get_inter_switch_flood_ports(dpid)
        blocked = {
            p for p in all_ports
            if self.topology_manager.is_inter_switch_port(dpid, p)
            and p not in tree_inter_switch_ports
        }
        return [p for p in all_ports if p != in_port and p not in blocked]
