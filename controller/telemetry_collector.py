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

Phase 10 adds the live prediction loop: each port-stats poll that lands
on a discovered inter-switch link is turned into a RawWindow (via
common/features.compute_base_metrics(), merging in the latest latency
probe for that link) and appended to a per-(link, switch-side) history,
matching ml/dataset/generate_dataset.py's granularity exactly so the
SAME compute_feature_vector() call produces comparable features live and
in training. The resulting feature vector is sent to
service/ml_rl_service.py via controller/inference_client.py; a None
result (service down, timeout, malformed response) means no prediction
this round -- no row is written to `predictions`, and no fallback value
is fabricated. Downstream consumers (Phase 12's persistence tracker) must
treat "no recent prediction" as inconclusive, not as healthy.

Phase 21 closes the loop. This module constructs the full decision chain
(controller/healing_loop.py: persistence -> stability -> remediation ->
verification, with the RL policy and its heuristic fallback underneath)
and feeds every successful prediction into it. It is built HERE rather
than inside healing_loop.py because only this module is guaranteed to
run under venv-ctrl, where `requests` -- needed by inference_client and
rl_client -- actually exists.

Two things the loop needs are supplied from this module's own live
state: the latest derived per-link metrics (cached as they are computed
for the ML pipeline, so path costing and prediction agree on what the
network looks like), and the managed flow's path, installed once LLDP
discovery has converged (_ensure_initial_path) so the controller KNOWS
which path traffic is on instead of inferring it from flood behaviour.
"""
from __future__ import annotations

import json
import struct
import time
from pathlib import Path

from common.config_loader import load_config
from common.features import RawWindow, compute_base_metrics, compute_feature_vector
from common.logging_setup import configure_logging
from common.schema import PROBE_ETH_TYPE, DataSource
from controller.flow_manager import FlowManager
from controller.healing_loop import HealingLoop
from controller.inference_client import InferenceClient
from controller.path_computer import LinkMetrics
from controller.persistence_tracker import PersistenceTracker
from controller.remediation_manager import FlowContext, RemediationManager
from controller.rl_client import RLClient
from controller.stability_manager import StabilityManager
from controller.verifier import Verifier
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
from network.topo_builder import load_topology_spec

logger = configure_logging("controller.telemetry_collector")

REPO_ROOT = Path(__file__).resolve().parent.parent

# OpenFlow reserved port numbers (OFPP_MAX and above) are never real
# switch-facing ports; skip them when polling/writing telemetry.
_OFPP_RESERVED_MIN = 0xFFFFFF00

_PROBE_HEADER = struct.Struct("!Qd")  # probe_id (uint64), send_ts (double)

# Bounds memory for per-(link, switch-side) RawWindow history. Comfortably
# larger than the biggest configured rolling window (features.windows,
# default max 10) -- history only needs to hold enough recent samples for
# compute_feature_vector(), not the whole run.
_MAX_HISTORY_LEN = 50


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

        # Live prediction loop state (Phase 10). link_capacities_bps comes
        # from the same topology YAML topo_builder.py used to build the
        # network -- the controller has no other way to learn a link's
        # configured bandwidth.
        topo_spec = load_topology_spec(cfg.network.topology_name)
        self._link_capacities_bps: dict[str, float] = {
            "-".join(sorted(link["endpoints"])): link["bw"] * 1_000_000
            for link in topo_spec["links"] if "bw" in link
        }
        self._link_history: dict[tuple[str, str], list[RawWindow]] = {}
        self._latest_latency_ms: dict[str, float] = {}
        self._features_config = cfg.features
        self._ml_threshold = cfg.ml.threshold
        self._label_horizon_s = cfg.label.horizon_s
        self._inference_client = InferenceClient(
            cfg.ml_rl_service.host, cfg.ml_rl_service.port, cfg.ml.inference_timeout_ms
        )

        # Phase 21: the closed loop. Everything below this point is what
        # turns a prediction into an actual reroute. Latest per-link
        # derived metrics are cached here (rather than re-queried from
        # SQLite) because the loop asks for them on every decision.
        self._latest_link_metrics: dict[str, LinkMetrics] = {}
        self._healing_loop = self._build_healing_loop(cfg)
        self._initial_path_installed = False
        self._initial_path_attempts = 0

        self._monitor_thread = hub.spawn(self._run_forever, self._poll_all_port_stats, self.interval_s)
        self._echo_thread = hub.spawn(self._run_forever, self._send_all_echo_requests, self.echo_interval_s)
        self._probe_thread = hub.spawn(self._run_forever, self._send_all_probes, self.interval_s)
        self._healing_thread = hub.spawn(self._run_forever, self._ensure_initial_path, self.interval_s)
        # Inference runs on its own greenthread, off the OpenFlow event
        # path, for the reason documented on _update_features.
        self._inference_thread = hub.spawn(self._run_forever, self._run_inference_cycle, self.interval_s)

    def _build_healing_loop(self, cfg) -> HealingLoop:
        """Constructs the full decision chain. RLClient is built here (not
        inside HealingLoop) because only this module is guaranteed to run
        under venv-ctrl, where `requests` exists."""
        flow_manager = FlowManager(
            self._db,
            priority=cfg.flow.priority_remediated,
            hard_timeout_s=cfg.flow.hard_timeout_s,
            idle_timeout_s=cfg.flow.idle_timeout_s,
        )
        # P1 vs P2 differ ONLY in who chooses the path, so P1 is exactly
        # P2 with no RL client -- the fallback the safety gate already
        # exercises. B1/B2 never consult the policy either.
        system = cfg.experiment.system
        rl_client = (
            RLClient(cfg.ml_rl_service.host, cfg.ml_rl_service.port, cfg.rl.timeout_ms)
            if system == "P2_ML_RL"
            else None
        )
        logger.info("running as system-under-test", extra={"system": system})
        remediation = RemediationManager(
            self._db, flow_manager,
            k_shortest=cfg.path.k_shortest,
            weight_utilization=cfg.path.weights.utilization,
            weight_latency=cfg.path.weights.latency,
            weight_loss=cfg.path.weights.loss,
            weight_hop_count=cfg.path.weights.hop_count,
            rl_client=rl_client,
        )
        return HealingLoop(
            db=self._db,
            persistence=PersistenceTracker(
                window=cfg.persistence.window,
                consecutive=cfg.persistence.consecutive,
                uncertainty_band=cfg.persistence.uncertainty_band,
                threshold=cfg.ml.threshold,
            ),
            stability=StabilityManager(
                cooldown_s=cfg.stability.cooldown_s,
                route_hold_s=cfg.stability.route_hold_s,
                min_improvement=cfg.stability.min_improvement,
            ),
            remediation=remediation,
            verifier=Verifier(
                self._db,
                settle_s=cfg.verify.settle_s,
                windows=cfg.verify.windows,
                latency_tolerance=cfg.verify.tolerance.latency_pct,
                loss_tolerance=cfg.verify.tolerance.loss_pct,
                max_retries=cfg.remediation.max_retries,
            ),
            flow_ctx=self._managed_flow_ctx(),
            graph_provider=lambda: self.topology_manager.graph,
            datapaths_provider=lambda: self.datapaths,
            link_metrics_provider=lambda: dict(self._latest_link_metrics),
            k_shortest=cfg.path.k_shortest,
            weight_utilization=cfg.path.weights.utilization,
            weight_latency=cfg.path.weights.latency,
            weight_loss=cfg.path.weights.loss,
            weight_hop_count=cfg.path.weights.hop_count,
            # Ryu runs on cooperative greenthreads: time.sleep would stall
            # every other switch's telemetry, and running a multi-second
            # verification inline inside an OpenFlow event handler would
            # do the same.
            sleep_fn=hub.sleep,
            spawn_fn=hub.spawn,
            system=system,
            b2_utilization_threshold=cfg.baselines.b2_utilization_threshold,
            b2_loss_threshold=cfg.baselines.b2_loss_threshold,
        )

    def _managed_flow_ctx(self) -> FlowContext:
        """The demand the loop protects: h1->h6, this project's flagship
        flow (network/traffic_generator.py and every dataset run use it).
        Host-facing ports are port 1 on each edge switch, matching how
        network/topo_builder.py attaches exactly one host per switch."""
        return FlowContext(
            flow_key="h1-h6",
            src_dpid=1,
            dst_dpid=6,
            src_host_port=1,
            dst_host_port=1,
            ipv4_src="10.0.0.1",
            ipv4_dst="10.0.0.6",
        )

    def _ensure_initial_path(self) -> None:
        """Install the managed flow's path once the topology has actually
        converged. Runs on the periodic loop rather than at startup
        because LLDP discovery needs several seconds after the switches
        connect before the graph is complete.

        Every not-yet-ready branch says what it is waiting for: a silent
        early return here is indistinguishable from a crash when the loop
        never installs a path, which is exactly the dead end the first
        live E2E run hit."""
        if self._initial_path_installed:
            return

        graph = self.topology_manager.graph
        ctx = self._healing_loop.flow_ctx
        self._initial_path_attempts += 1
        # Throttled: this runs every telemetry interval, and a converging
        # network is normal for the first several seconds.
        should_log = self._initial_path_attempts % 5 == 1

        if ctx.src_dpid not in graph or ctx.dst_dpid not in graph:
            if should_log:
                logger.info(
                    "waiting to install initial path: endpoints not discovered yet",
                    extra={
                        "src_dpid": ctx.src_dpid, "dst_dpid": ctx.dst_dpid,
                        "known_switches": sorted(graph.nodes()),
                        "connected_datapaths": sorted(self.datapaths),
                    },
                )
            return

        if len(self.datapaths) < graph.number_of_nodes():
            if should_log:
                logger.info(
                    "waiting to install initial path: not every switch has connected",
                    extra={
                        "connected": len(self.datapaths),
                        "discovered": graph.number_of_nodes(),
                    },
                )
            return

        if self._healing_loop.install_initial_path() is not None:
            self._initial_path_installed = True

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
            self._prev_counters[key] = row
            if prev is not None:
                dt = now - prev["timestamp"]
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

                if row["link_id"] is not None:
                    self._update_features(row["link_id"], str(dpid), prev, row)

    def _update_features(self, link_id: str, switch_id: str, prev_row: dict, curr_row: dict) -> None:
        """Cheap, I/O-free: derive metrics and append to history.

        Inference deliberately does NOT happen here. This runs inside the
        OpenFlow event handler, and Phase 21 measured that issuing HTTP
        inference calls from this path inflated the controller's own
        latency-probe measurements ~20x (23 ms -> 464 ms), corrupting the
        latency-slope features the model depends on most. Scoring is done
        by _run_inference_cycle on its own greenthread instead. See
        docs/limitations.md.
        """
        dt = curr_row["timestamp"] - prev_row["timestamp"]
        curr_with_latency = dict(curr_row)
        curr_with_latency["latency_ms"] = self._latest_latency_ms.get(link_id)

        base = compute_base_metrics(
            prev_row, curr_with_latency, dt, self._link_capacities_bps.get(link_id)
        )
        if base is None:
            return

        # Keep the healing loop's view of live per-link conditions fresh.
        # These are the same derived metrics the ML pipeline consumes, so
        # path costing and prediction agree on what the network looks like.
        self._latest_link_metrics[link_id] = LinkMetrics(
            utilization=base.utilization,
            latency_ms=base.latency_ms,
            loss_rate=base.loss_rate,
        )

        # Persist the derived metrics, not just the raw counters. Until
        # Phase 22 these lived only in memory, so `features.utilization`
        # was NULL for every row and there was no record of what the
        # system actually saw when it made a decision -- which made
        # post-hoc analysis of an experiment impossible and left the
        # dashboard's link inspector permanently blank.
        self._db.insert(
            "features",
            {
                "timestamp": curr_row["timestamp"],
                "link_id": link_id,
                "window_s": int(self.interval_s),
                "utilization": base.utilization,
                "loss_rate": base.loss_rate,
                "error_rate": base.error_rate,
                "latency_ms": base.latency_ms,
                "latency_is_estimate": 1,
                "feature_json": json.dumps(
                    {"switch_id": switch_id, "packet_rate": base.packet_rate}
                ),
                "data_source": DataSource.EMULATED_MININET.value,
            },
        )

        history_key = (link_id, switch_id)
        history = self._link_history.setdefault(history_key, [])
        history.append(base)
        if len(history) > _MAX_HISTORY_LEN:
            del history[: len(history) - _MAX_HISTORY_LEN]

    def _run_inference_cycle(self) -> None:
        """Scores every link that has enough history, in ONE batched call,
        off the OpenFlow event path.

        The safety gate is unchanged and now applies to the whole batch:
        if the service is down, slow, or returns anything unusable, NO
        predictions are recorded for this cycle and nothing is
        fabricated. Downstream must treat "no recent prediction" as
        inconclusive, never as healthy."""
        snapshot = [
            (link_id, switch_id, list(history))
            for (link_id, switch_id), history in self._link_history.items()
            if history
        ]
        if not snapshot:
            return

        items = []
        for link_id, _switch_id, history in snapshot:
            feature_vector = compute_feature_vector(
                history, len(history) - 1,
                windows=self._features_config.windows,
                ewma_alpha=self._features_config.ewma_alpha,
                baseline_windows=self._features_config.baseline_healthy_windows,
            )
            items.append((link_id, feature_vector))

        result = self._inference_client.predict_degradation_batch(items)
        if result is None:
            return  # safety gate: no predictions this round

        pairs, model_version = result

        # Both endpoints of a link are scored (each switch reports its own
        # side's counters), but a LINK has one health state, so collapse
        # them into a single reading per cycle -- taking the max, since
        # either endpoint observing degradation is evidence of it.
        #
        # Without this the persistence tracker receives ~2 readings per
        # telemetry cycle, so `persistence.window = 4` spans only ~4s
        # rather than the 4 telemetry windows (~8s) the spec's "3 of the
        # last 4 windows" means. Measured live: 160 readings over 168s =
        # 1.9 per cycle. This also stops one link's two sides from
        # counting as independent corroboration of each other, which is
        # exactly what the persistence gate exists to require.
        per_link: dict[str, float] = {}
        for link_id, probability in pairs:
            if probability > per_link.get(link_id, -1.0):
                per_link[link_id] = probability

        now = time.time()
        for link_id, probability in per_link.items():
            self._db.insert(
                "predictions",
                {
                    "timestamp": now,
                    "link_id": link_id,
                    "probability": probability,
                    "threshold": self._ml_threshold,
                    "model_version": model_version,
                    "horizon_s": self._label_horizon_s,
                },
            )
            # Hand the prediction to the closed loop. A failure in the
            # decision chain must never take down telemetry collection --
            # the loop is what heals the network, but telemetry is what
            # lets anyone see it at all.
            try:
                self._healing_loop.on_prediction(link_id, probability, now)
            except Exception:
                logger.exception("healing loop raised on prediction", extra={"link_id": link_id})

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
        self._latest_latency_ms[link_id] = latency_estimate_s * 1000.0
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
