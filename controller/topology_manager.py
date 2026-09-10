"""Live topology discovery via Ryu's built-in LLDP-based link discovery.

Requires ryu-manager to be launched with --observe-links so ryu.topology's
own switches app is loaded and emits EventSwitchEnter/EventLinkAdd/etc.

Maintains a networkx.Graph of the discovered network (switches as nodes,
inter-switch links as edges carrying each endpoint's port number) and
recomputes a minimum spanning tree whenever the topology changes. The
spanning tree is what main_app.py's L2 forwarding uses to flood
broadcast/unknown-unicast traffic safely: network/topologies/*.yaml
topologies have real L2 cycles by design (redundant paths), so flooding on
every inter-switch link would storm exactly like Phase 3's OVS-standalone
pingall did before RSTP was added there (see docs/decisions.md,
2026-08-31) -- here the same problem is solved controller-side instead,
since the controller is now the one deciding forwarding. Real shortest/
RL-selected-path forwarding replaces flooding entirely from Phase 11
onward; until then, spanning-tree-restricted flooding is what keeps this
phase's "hosts ping" gate loop-free.

Also persists a topology_snapshots row (source='discovered') on every
change, so the live graph can be compared against the YAML topology spec
that built the emulated network (Phase 4's other gate condition).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import networkx as nx

from common.config_loader import load_config
from common.logging_setup import configure_logging
from common.schema import LinkHealthState
from controller.of_compat import app_manager, get_link, get_switch, set_ev_cls, topo_event
from database.db import Database
from network.topo_builder import load_topology_spec

logger = configure_logging("controller.topology_manager")

REPO_ROOT = Path(__file__).resolve().parent.parent


class TopologyManager(app_manager.RyuApp):
    """Ryu app context (see main_app.py's _CONTEXTS) tracking the live
    topology graph and the flood-safe spanning tree derived from it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.graph: nx.Graph = nx.Graph()
        self.spanning_tree_edges: set[frozenset] = set()
        # Callbacks fired when a discovered link disappears. The healing
        # loop registers here so a hard failure triggers remediation
        # directly: the ML predictor cannot see one (a dead link looks
        # idle, and its labels only cover degradation that has yet to
        # happen), so LLDP's own removal event is the signal.
        self._link_removed_callbacks: list = []

        cfg = load_config()
        self._db = Database(REPO_ROOT / cfg.database.path, wal=cfg.database.wal)
        # Configured capacities come from the same topology YAML the
        # network was built from; OpenFlow does not report a link's
        # provisioned bandwidth and LLDP does not carry it.
        try:
            topo_spec = load_topology_spec(cfg.network.topology_name)
            self._link_capacities_bps = {
                "-".join(sorted(link["endpoints"])): link["bw"] * 1_000_000
                for link in topo_spec["links"] if "bw" in link
            }
        except Exception:
            logger.exception("could not load topology spec for link capacities")
            self._link_capacities_bps = {}

    def register_link_removed_callback(self, callback) -> None:
        self._link_removed_callbacks.append(callback)

    @set_ev_cls(topo_event.EventSwitchEnter)
    def _switch_enter(self, ev):
        self._refresh_topology()

    @set_ev_cls(topo_event.EventSwitchLeave)
    def _switch_leave(self, ev):
        self._refresh_topology()

    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add(self, ev):
        self._refresh_topology()

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_delete(self, ev):
        self._refresh_topology()

    def _refresh_topology(self) -> None:
        switch_list = get_switch(self, None)
        link_list = get_link(self, None)

        graph = nx.Graph()
        for switch in switch_list:
            graph.add_node(switch.dp.id)
        for link in link_list:
            graph.add_edge(
                link.src.dpid,
                link.dst.dpid,
                ports={link.src.dpid: link.src.port_no, link.dst.dpid: link.dst.port_no},
            )

        previous_edges = {
            "-".join(sorted((f"s{u}", f"s{v}"))) for u, v in self.graph.edges()
        }
        current_edges = {
            "-".join(sorted((f"s{u}", f"s{v}"))) for u, v in graph.edges()
        }

        self.graph = graph
        for link_id in sorted(previous_edges - current_edges):
            logger.info("link disappeared from topology", extra={"link_id": link_id})
            for callback in self._link_removed_callbacks:
                try:
                    callback(link_id)
                except Exception:
                    # One listener failing must never stop topology
                    # tracking; discovery is what everything else needs.
                    logger.exception("link-removed callback failed", extra={"link_id": link_id})

        if graph.number_of_edges() > 0:
            mst = nx.minimum_spanning_tree(graph)
            self.spanning_tree_edges = {frozenset(e) for e in mst.edges()}
        else:
            self.spanning_tree_edges = set()

        self._persist_snapshot()
        logger.info(
            "topology updated",
            extra={
                "num_switches": graph.number_of_nodes(),
                "num_links": graph.number_of_edges(),
                "spanning_tree_edges": len(self.spanning_tree_edges),
            },
        )

    def _persist_snapshot(self) -> None:
        data = nx.node_link_data(self.graph)
        self._db.insert(
            "topology_snapshots",
            {
                "timestamp": time.time(),
                "topology_json": json.dumps(data),
                "source": "discovered",
            },
        )
        self._upsert_links()

    def _upsert_links(self) -> None:
        """Keep the `links` registry in step with what LLDP has actually
        discovered.

        This table was defined in Phase 1 but never written by anything,
        so it stayed empty for the whole project: the dashboard's map had
        no link health to colour by, and its status banner -- which keys
        off this table -- reported "waiting for the network" even while
        showing a completed incident from the same database. Structural
        fields come from the discovered graph; `state` is owned by the
        healing loop, so an existing row's state is preserved here rather
        than being reset to HEALTHY on every topology refresh.
        """
        now = time.time()
        for u, v in self.graph.edges():
            ports = self.graph[u][v].get("ports", {})
            link_id = "-".join(sorted((f"s{u}", f"s{v}")))
            row = self._db.query_one("SELECT link_id FROM links WHERE link_id = ?", (link_id,))
            if row is None:
                self._db.insert(
                    "links",
                    {
                        "link_id": link_id,
                        "src_switch": f"s{u}",
                        "src_port": ports.get(u, 0),
                        "dst_switch": f"s{v}",
                        "dst_port": ports.get(v, 0),
                        "capacity_bps": self._link_capacities_bps.get(link_id),
                        "state": LinkHealthState.HEALTHY.value,
                        "last_updated": now,
                    },
                )
            else:
                self._db.execute(
                    "UPDATE links SET src_switch=?, src_port=?, dst_switch=?, dst_port=?, "
                    "capacity_bps=?, last_updated=? WHERE link_id=?",
                    (f"s{u}", ports.get(u, 0), f"s{v}", ports.get(v, 0),
                     self._link_capacities_bps.get(link_id), now, link_id),
                )

    def get_all_ports(self, dpid: int) -> set[int]:
        """Every port Ryu knows about on `dpid` (host-facing and
        inter-switch alike), via ryu.topology's own port tracking."""
        switches = get_switch(self, dpid)
        if not switches:
            return set()
        return {port.port_no for port in switches[0].ports}

    def is_inter_switch_port(self, dpid: int, port_no: int) -> bool:
        """True if `port_no` on `dpid` is a discovered switch-switch link
        (as opposed to a host-facing port, which LLDP discovery never
        reports as a link)."""
        if dpid not in self.graph:
            return False
        for neighbor in self.graph.neighbors(dpid):
            if self.graph[dpid][neighbor]["ports"][dpid] == port_no:
                return True
        return False

    def get_inter_switch_flood_ports(self, dpid: int) -> set[int]:
        """Inter-switch ports on `dpid` that are safe to flood on: only
        those on a spanning-tree edge. Never includes host-facing ports
        (those aren't in self.graph at all -- flooding on them is always
        safe and handled separately by the caller)."""
        ports = set()
        if dpid not in self.graph:
            return ports
        for neighbor in self.graph.neighbors(dpid):
            if frozenset((dpid, neighbor)) in self.spanning_tree_edges:
                ports.add(self.graph[dpid][neighbor]["ports"][dpid])
        return ports
