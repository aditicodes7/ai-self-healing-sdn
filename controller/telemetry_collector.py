"""Periodic telemetry collection: OFPPortStatsRequest polling plus a
controller-emitted latency probe per discovered link.

Raw counters (tx/rx bytes/packets, drops, errors) are polled every
telemetry.interval_s and written to `telemetry` as-is (data_source=
emulated_mininet); no derived metrics (utilization, loss_rate, ...) are
computed here -- that is common/features.py (Phase 6), fed by these rows.

Latency is an ESTIMATE, not ground truth (see docs/architecture.md): a
probe frame (EtherType common.schema.PROBE_ETH_TYPE) carrying a send
timestamp is sent out one switch's port toward a directly-connected
neighbor over a discovered link; arrival is a PacketIn on the neighbor's
port. The elapsed time includes two controller<->switch control-channel
hops (the packet-out to the sender, the packet-in from the receiver) on
top of the physical link delay we actually want, so half of an
independently-measured OFPEchoRequest RTT is subtracted as an
approximation of one control-channel hop. Phase 5's acceptance gate
validates this estimate is in the right ballpark against real `ping`
ground truth (network/topologies/*.yaml link `delay` values).

Each probe result is written as a row in `features` with only
link_id/timestamp/latency_ms/latency_is_estimate/data_source populated
(window_s=0, feature_json='{}') since features.py doesn't own real
windowed aggregation until Phase 6 -- this is an honest raw sample, not a
fabricated windowed feature.
"""
from __future__ import annotations

import json
import struct
import time
from pathlib import Path

from common.config_loader import load_config
from common.logging_setup import configure_logging
from common.schema import PROBE_ETH_TYPE, DataSource
from controller.of_compat import (
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    app_manager,
    ethernet,
    hub,
    ofp_event,
    packet,
    set_ev_cls,
)
from controller.topology_manager import TopologyManager
from database.db import Database

logger = configure_logging("controller.telemetry_collector")

REPO_ROOT = Path(__file__).resolve().parent.parent

# OpenFlow reserved port numbers (OFPP_MAX and above) are never real
# switch-facing ports; skip them when polling/writing telemetry.
_OFPP_RESERVED_MIN = 0xFFFFFF00

_PROBE_HEADER = struct.Struct("!Qd")  # probe_id (uint64), send_ts (double)


class TelemetryCollector(app_manager.RyuApp):
    _CONTEXTS = {"topology_manager": TopologyManager}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.topology_manager: TopologyManager = kwargs["topology_manager"]

        cfg = load_config()
        self.interval_s = cfg.telemetry.interval_s
        self.echo_interval_s = cfg.telemetry.echo_interval_s
        self._db = Database(REPO_ROOT / cfg.database.path, wal=cfg.database.wal)

        self.datapaths: dict[int, object] = {}
        self._prev_counters: dict[tuple[int, int], dict] = {}
        self.echo_rtt_s: dict[int, float] = {}
        self._pending_probes: dict[tuple[int, int, int], float] = {}
        self._probe_seq = 0

        self._monitor_thread = hub.spawn(self._run_forever, self._poll_all_port_stats, self.interval_s)
        self._echo_thread = hub.spawn(self._run_forever, self._send_all_echo_requests, self.echo_interval_s)
        self._probe_thread = hub.spawn(self._run_forever, self._send_all_probes, self.interval_s)

    def _run_forever(self, iteration_fn, interval_s: float) -> None:
        """A single buggy iteration must never silently kill the whole
        background loop (confirmed the hard way: a bug here previously
        crashed _probe_loop on its very first tick, with no visible symptom
        besides zero latency rows ever appearing)."""
        while True:
            try:
                iteration_fn()
            except Exception:
                logger.exception("periodic task iteration failed", extra={"task": iteration_fn.__name__})
            hub.sleep(interval_s)

    # -- datapath lifecycle -------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
            logger.info("datapath registered for telemetry", extra={"dpid": datapath.id})
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)

    # -- port stats polling --------------------------------------------------

    def _poll_all_port_stats(self) -> None:
        for datapath in list(self.datapaths.values()):
            parser = datapath.ofproto_parser
            ofproto = datapath.ofproto
            datapath.send_msg(parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()

        for stat in ev.msg.body:
            if stat.port_no >= _OFPP_RESERVED_MIN:
                continue

            row = {
                "timestamp": now,
                "switch_id": str(dpid),
                "port_no": stat.port_no,
                "link_id": self._link_id_for(dpid, stat.port_no),
                "tx_bytes": stat.tx_bytes,
                "rx_bytes": stat.rx_bytes,
                "tx_packets": stat.tx_packets,
                "rx_packets": stat.rx_packets,
                "rx_dropped": stat.rx_dropped,
                "tx_dropped": stat.tx_dropped,
                "rx_errors": stat.rx_errors,
                "tx_errors": stat.tx_errors,
                "duration_sec": stat.duration_sec + stat.duration_nsec / 1e9,
                "data_source": DataSource.EMULATED_MININET.value,
            }
            self._db.insert("telemetry", row)

            key = (dpid, stat.port_no)
            prev = self._prev_counters.get(key)
            self._prev_counters[key] = {"t": now, "tx_bytes": stat.tx_bytes, "rx_bytes": stat.rx_bytes}
            if prev is not None:
                dt = now - prev["t"]
                delta_rx = stat.rx_bytes - prev["rx_bytes"]
                delta_tx = stat.tx_bytes - prev["tx_bytes"]
                # Counter reset (switch restarted, or 64-bit wrap): don't
                # report a nonsensical negative delta.
                if dt > 0 and delta_rx >= 0 and delta_tx >= 0:
                    logger.info(
                        "telemetry",
                        extra={
                            "dpid": dpid, "port_no": stat.port_no, "link_id": row["link_id"],
                            "rx_bytes_per_s": delta_rx / dt, "tx_bytes_per_s": delta_tx / dt,
                        },
                    )

    def _link_id_for(self, dpid: int, port_no: int) -> str | None:
        graph = self.topology_manager.graph
        if dpid not in graph:
            return None
        for neighbor in graph.neighbors(dpid):
            if graph[dpid][neighbor]["ports"][dpid] == port_no:
                a, b = sorted((f"s{dpid}", f"s{neighbor}"))
                return f"{a}-{b}"
        return None

    # -- echo RTT (used to correct the latency probe estimate) --------------

    def _send_all_echo_requests(self) -> None:
        for datapath in list(self.datapaths.values()):
            parser = datapath.ofproto_parser
            datapath.send_msg(parser.OFPEchoRequest(datapath, data=struct.pack("!d", time.time())))

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def _echo_reply_handler(self, ev):
        try:
            (sent_ts,) = struct.unpack("!d", ev.msg.data)
        except struct.error:
            return
        self.echo_rtt_s[ev.msg.datapath.id] = time.time() - sent_ts

    # -- latency probing ------------------------------------------------------

    def _send_all_probes(self) -> None:
        for src_dpid, dst_dpid in list(self.topology_manager.graph.edges()):
            self._send_probe(src_dpid, dst_dpid)
            self._send_probe(dst_dpid, src_dpid)

    def _send_probe(self, src_dpid: int, dst_dpid: int) -> None:
        datapath = self.datapaths.get(src_dpid)
        if datapath is None:
            return
        graph = self.topology_manager.graph
        if not graph.has_edge(src_dpid, dst_dpid):
            return
        out_port = graph[src_dpid][dst_dpid]["ports"][src_dpid]

        self._probe_seq += 1
        probe_id = self._probe_seq
        send_ts = time.time()
        self._pending_probes[(src_dpid, dst_dpid, probe_id)] = send_ts

        eth = ethernet.ethernet(
            dst="ff:ff:ff:ff:ff:ff",
            src=f"02:00:00:00:{src_dpid:02x}:{dst_dpid:02x}",
            ethertype=PROBE_ETH_TYPE,
        )
        pkt = packet.Packet()
        pkt.add_protocol(eth)
        pkt.serialize()
        data = pkt.data + _PROBE_HEADER.pack(probe_id, send_ts)

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(out_port)], data=data,
        )
        datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype != PROBE_ETH_TYPE:
            return

        recv_ts = time.time()
        # The probe header is appended as the very last bytes of the frame
        # in _send_probe(), regardless of header length -- read it from the
        # end, not from a fixed offset after the Ethernet header. Ryu's
        # packet.Packet.serialize() pads a bare Ethernet frame with no
        # payload up to the 60-byte minimum frame size, so a fixed offset
        # (e.g. right after a 14-byte header) actually lands inside that
        # zero padding, not our real data -- confirmed empirically (every
        # parsed probe_id/send_ts came back as exactly 0/0.0).
        if len(msg.data) < _PROBE_HEADER.size:
            return
        payload = msg.data[-_PROBE_HEADER.size:]
        probe_id, send_ts = _PROBE_HEADER.unpack(payload)

        dst_dpid = msg.datapath.id
        matched_key = next(
            (k for k in self._pending_probes if k[1] == dst_dpid and k[2] == probe_id), None
        )
        if matched_key is None:
            return
        src_dpid = matched_key[0]
        sent_ts = self._pending_probes.pop(matched_key)

        elapsed_s = recv_ts - sent_ts
        echo_rtt_s = self.echo_rtt_s.get(dst_dpid, 0.0)
        latency_estimate_s = max(elapsed_s - 0.5 * echo_rtt_s, 0.0)

        a, b = sorted((f"s{src_dpid}", f"s{dst_dpid}"))
        link_id = f"{a}-{b}"
        self._db.insert(
            "features",
            {
                "timestamp": recv_ts,
                "link_id": link_id,
                "window_s": 0,
                "latency_ms": latency_estimate_s * 1000.0,
                "latency_is_estimate": 1,
                "feature_json": json.dumps({"probe_id": probe_id, "elapsed_s": elapsed_s, "echo_rtt_s": echo_rtt_s}),
                "data_source": DataSource.EMULATED_MININET.value,
            },
        )
        logger.info(
            "latency probe",
            extra={"link_id": link_id, "latency_ms": latency_estimate_s * 1000.0, "echo_rtt_ms": echo_rtt_s * 1000.0},
        )
