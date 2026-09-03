"""Fast, seeded, pure-numpy queueing/utilization simulator of the SAME
topology used throughout this project (network/topologies/*.yaml),
standing in for real Mininet during RL training. PPO's step counts
(200k-500k, Phase 17) are infeasible against real Mininet -- each step
would need real wall-clock network I/O. This substitution is stated
openly, not hidden: see docs/limitations.md and rl/calibrate.py's real
measured comparison against actual Mininet throughput/latency for
identical scenarios (Phase 16's gate).

Per-link model -- a standard M/M/1-style queueing approximation, NOT a
packet-level simulation (that's exactly the fidelity/speed trade-off
being made and disclosed):
  utilization        = aggregate offered load across every flow sharing
                        this link, divided by effective capacity (capped
                        just under 1 so queueing delay stays finite)
  queueing_delay_ms   = (u / (1-u)) * service_time_ms, the standard M/M/1
                        mean-queueing-delay formula, where service_time_ms
                        is a reference-packet-size / capacity term
  latency_ms          = configured propagation delay + any fault's extra
                        delay + queueing_delay_ms
  loss_rate           = droptail approximation: the fraction of offered
                        load beyond capacity is dropped, compounded with
                        any fault's independent loss rate
A flow's throughput on a shared/congested link is its proportional fair
share of capacity (flow's own offered load / total offered load on that
link) -- not a full TCP congestion-control model, which is out of scope
for a "fast" trainer; flows here behave like fixed-rate (UDP-like)
traffic, matching how this project's own background traffic generation
works everywhere else (network/traffic_generator.py, generate_dataset.py).

Multi-hop paths compose: end-to-end throughput = min over hops (the
bottleneck), latency = sum over hops, loss = 1 - product(1 - per-hop
loss).
"""
from __future__ import annotations

import dataclasses

import numpy as np

# A reference MTU-ish packet size used only to convert a link's capacity
# into a "service time" for the M/M/1 delay formula -- not a claim about
# the real packet-size distribution, which packet-level simulation would
# need and this deliberately does not model.
_REFERENCE_PACKET_BITS = 12_000  # ~1500 bytes


def _parse_delay_ms(delay_str) -> float:
    if not delay_str:
        return 0.0
    return float(str(delay_str).replace("ms", "").strip())


@dataclasses.dataclass
class LinkState:
    capacity_bps: float
    propagation_delay_ms: float
    fault_capacity_multiplier: float = 1.0  # < 1.0 = a congestion-style fault
    fault_extra_delay_ms: float = 0.0       # a high_latency-style fault
    fault_loss_rate: float = 0.0            # a packet_loss/rising_errors-style fault
    fault_down: bool = False                # a link_failure-style fault

    @property
    def effective_capacity_bps(self) -> float:
        return 0.0 if self.fault_down else self.capacity_bps * self.fault_capacity_multiplier


def link_aggregate_metrics(total_offered_bps: float, link: LinkState) -> tuple[float, float]:
    """(latency_ms, loss_rate) for a link given the AGGREGATE offered load
    of every flow currently sharing it -- both are properties of the
    link's own shared queue, not any individual flow."""
    if link.fault_down:
        return float("inf"), 1.0

    capacity = max(link.effective_capacity_bps, 1.0)
    utilization = min(total_offered_bps / capacity, 0.999)

    service_time_ms = (_REFERENCE_PACKET_BITS / capacity) * 1000.0
    queueing_delay_ms = (utilization / (1.0 - utilization)) * service_time_ms
    latency_ms = link.propagation_delay_ms + link.fault_extra_delay_ms + queueing_delay_ms

    capacity_loss = max(0.0, total_offered_bps - capacity) / total_offered_bps if total_offered_bps > 0 else 0.0
    loss_rate = 1.0 - (1.0 - capacity_loss) * (1.0 - link.fault_loss_rate)

    return latency_ms, loss_rate


def flow_share_of_link(flow_offered_bps: float, total_offered_bps: float, link: LinkState) -> float:
    """This flow's achieved throughput on one link: its full offered load
    if the link isn't oversubscribed, otherwise its proportional fair
    share of capacity."""
    if link.fault_down:
        return 0.0
    capacity = max(link.effective_capacity_bps, 1.0)
    if total_offered_bps <= capacity or total_offered_bps <= 0:
        return flow_offered_bps
    return capacity * (flow_offered_bps / total_offered_bps)


class NetworkSimulator:
    """Seeded, stateful simulator over discrete steps. Tracks active flows
    (each a fixed offered load routed along a path) and per-link fault
    state; step() aggregates offered load per link from every flow
    sharing it and returns per-flow and per-link metrics for that step."""

    def __init__(self, topology_spec: dict, seed: int):
        self.rng = np.random.default_rng(seed)
        self.links: dict[str, LinkState] = {}
        self._edge_to_link_id: dict[tuple[int, int], str] = {}
        for link in topology_spec["links"]:
            sw_a, sw_b = link["endpoints"]
            link_id = "-".join(sorted((sw_a, sw_b)))
            self.links[link_id] = LinkState(
                capacity_bps=link["bw"] * 1_000_000,
                propagation_delay_ms=_parse_delay_ms(link.get("delay", "0ms")),
            )
            a = int(sw_a.lstrip("s"))
            b = int(sw_b.lstrip("s"))
            self._edge_to_link_id[(a, b)] = link_id
            self._edge_to_link_id[(b, a)] = link_id

        self.flows: dict[str, dict] = {}  # flow_key -> {"path": [...], "offered_load_bps": float}

    def link_id_for_edge(self, u: int, v: int) -> str:
        return self._edge_to_link_id[(u, v)]

    def set_flow(self, flow_key: str, path: list[int], offered_load_bps: float) -> None:
        self.flows[flow_key] = {"path": path, "offered_load_bps": offered_load_bps}

    def remove_flow(self, flow_key: str) -> None:
        self.flows.pop(flow_key, None)

    def inject_fault(
        self, link_id: str, capacity_multiplier: float = 1.0,
        extra_delay_ms: float = 0.0, loss_rate: float = 0.0, down: bool = False,
    ) -> None:
        link = self.links[link_id]
        link.fault_capacity_multiplier = capacity_multiplier
        link.fault_extra_delay_ms = extra_delay_ms
        link.fault_loss_rate = loss_rate
        link.fault_down = down

    def clear_fault(self, link_id: str) -> None:
        self.inject_fault(link_id)

    def step(self) -> dict:
        """Returns {"links": {link_id: {"utilization", "latency_ms",
        "loss_rate"}}, "flows": {flow_key: {"throughput_bps",
        "latency_ms", "loss_rate"}}}."""
        link_total_offered: dict[str, float] = {lid: 0.0 for lid in self.links}
        for flow in self.flows.values():
            path = flow["path"]
            for u, v in zip(path, path[1:]):
                lid = self.link_id_for_edge(u, v)
                link_total_offered[lid] += flow["offered_load_bps"]

        link_results = {}
        for lid, link in self.links.items():
            total = link_total_offered[lid]
            latency_ms, loss_rate = link_aggregate_metrics(total, link)
            capacity = max(link.effective_capacity_bps, 1.0)
            utilization = 0.0 if link.fault_down else min(total / capacity, 1.0)
            link_results[lid] = {"utilization": utilization, "latency_ms": latency_ms, "loss_rate": loss_rate}

        flow_results = {}
        for flow_key, flow in self.flows.items():
            path = flow["path"]
            offered = flow["offered_load_bps"]
            hop_throughputs, hop_latencies, hop_losses = [], [], []
            for u, v in zip(path, path[1:]):
                lid = self.link_id_for_edge(u, v)
                link = self.links[lid]
                total = link_total_offered[lid]
                hop_throughputs.append(flow_share_of_link(offered, total, link))
                hop_latencies.append(link_results[lid]["latency_ms"])
                hop_losses.append(link_results[lid]["loss_rate"])

            if hop_throughputs:
                throughput_bps = min(hop_throughputs)
                latency_ms = sum(hop_latencies)
                loss_rate = 1.0 - float(np.prod([1.0 - loss for loss in hop_losses]))
            else:
                throughput_bps, latency_ms, loss_rate = 0.0, 0.0, 0.0

            flow_results[flow_key] = {
                "throughput_bps": throughput_bps, "latency_ms": latency_ms, "loss_rate": loss_rate,
            }

        return {"links": link_results, "flows": flow_results}
