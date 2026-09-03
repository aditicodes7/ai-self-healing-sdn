#!/usr/bin/env python3
"""Compares rl/env/simulator.py's predictions against real Mininet
measurements for identical scenarios (same link capacity/delay, same
offered load), and writes results/tables/simulator_calibration.json.

This is the disclosure Phase 16 requires: RL training (Phase 17) uses the
pure-numpy simulator because PPO's step counts (200k-500k) are infeasible
against real Mininet, and this script is what lets that substitution be
backed by a real measured comparison instead of an unverified assumption.
See docs/limitations.md for the full discussion.

Scenarios use single-hop host pairs (h1-h2 via s1-s2, h2-h3 via s2-s3) at
several offered UDP rates, including near/at link capacity (where
queueing delay and loss both become significant) -- exactly where a
simulator's fidelity matters most. Real throughput/loss come from
iperf3's own JSON (-J) output; real latency comes from independent
`ping`, deliberately not the controller's own probe estimate (see
docs/limitations.md's Phase 5 entry on why that estimate is noisy) so
this calibration isn't checking the simulator against another estimate.

Must run under the VM's SYSTEM Python 3 (needs Mininet + root), same as
topo_builder.py/traffic_generator.py.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rl.env.simulator import LinkState, link_aggregate_metrics  # noqa: E402

# (client, server, link_id, capacity_mbit, delay_ms, offered_mbit)
SCENARIOS = [
    ("h1", "h2", "s1-s2", 10, 2.0, 2),   # well under capacity
    ("h1", "h2", "s1-s2", 10, 2.0, 8),   # near capacity -- queueing matters
    ("h1", "h2", "s1-s2", 10, 2.0, 15),  # over capacity -- loss matters
    ("h2", "h3", "s2-s3", 10, 2.0, 5),   # different link, mid load
]


def _simulate(capacity_mbit: float, delay_ms: float, offered_mbit: float) -> dict:
    link = LinkState(capacity_bps=capacity_mbit * 1_000_000, propagation_delay_ms=delay_ms)
    latency_ms, loss_rate = link_aggregate_metrics(offered_mbit * 1_000_000, link)
    achieved_mbit = min(offered_mbit, capacity_mbit) * (1 - loss_rate) if offered_mbit > 0 else 0.0
    return {"throughput_mbit": achieved_mbit, "latency_ms": latency_ms, "loss_rate": loss_rate}


def _measure_real(net, client_id: str, server_id: str, offered_mbit: float, duration_s: int = 5) -> dict:
    from network.traffic_generator import run_iperf_client, start_iperf_server, stop_iperf_server

    client = net.get(client_id)
    server = net.get(server_id)

    server_proc = start_iperf_server(server, port=5301)
    time.sleep(1)
    result = run_iperf_client(
        client, server.IP(), port=5301, duration_s=duration_s,
        protocol="udp", bandwidth=f"{offered_mbit}M",
    )
    stop_iperf_server(server_proc)

    end = result.get("end", {})
    sum_block = end.get("sum", end.get("sum_received", {}))
    # For a UDP test, iperf3's "sum" block reports the SENDER's offered
    # rate in bits_per_second (UDP has no flow control, so the sender
    # just transmits at the requested -b rate regardless of what actually
    # arrives) -- lost_percent is the separately, correctly computed
    # drop fraction. Actual received throughput = offered * (1 - loss).
    # Confirmed empirically (2026-09-03): at 15Mbit offered on a 10Mbit
    # link, this field read ~14.99Mbit with lost_percent~20%, which is
    # only consistent with it being the send rate, not the receive rate.
    offered_bps = sum_block.get("bits_per_second", 0.0)
    loss_percent = sum_block.get("lost_percent", 0.0)
    achieved_bps = offered_bps * (1 - loss_percent / 100.0)

    # ping measures ROUND-TRIP time; the simulator's latency_ms is
    # one-way (matches network/topologies/*.yaml's per-link `delay`,
    # which TCLink applies as one-way propagation delay). Halve here so
    # both sides of the comparison mean the same physical quantity.
    ping_out = client.cmd(f"ping -c 4 {server.IP()}")
    rtts = []
    for line in ping_out.splitlines():
        if "time=" in line:
            rtts.append(float(line.split("time=")[1].split()[0]))
    latency_ms = (sum(rtts) / len(rtts)) / 2.0 if rtts else None

    return {
        "throughput_mbit": achieved_bps / 1_000_000,
        "latency_ms": latency_ms,
        "loss_rate": loss_percent / 100.0,
    }


def main() -> int:
    from mininet.log import setLogLevel

    from network.topo_builder import build_network, load_topology_spec

    setLogLevel("info")
    spec = load_topology_spec("scenario_a")
    net = build_network(spec, "127.0.0.1", 6653, fail_mode="secure")
    net.start()

    comparisons = []
    try:
        net.pingAll()
        for client_id, server_id, link_id, capacity_mbit, delay_ms, offered_mbit in SCENARIOS:
            print(f"=== {client_id}->{server_id} ({link_id}), offered={offered_mbit}Mbit ===", flush=True)
            real = _measure_real(net, client_id, server_id, offered_mbit)
            sim = _simulate(capacity_mbit, delay_ms, offered_mbit)

            def rel_error(sim_v, real_v):
                if real_v is None or sim_v is None:
                    return None
                if abs(real_v) < 1e-9:
                    return None
                return abs(sim_v - real_v) / abs(real_v)

            comparisons.append(
                {
                    "client": client_id, "server": server_id, "link_id": link_id,
                    "capacity_mbit": capacity_mbit, "delay_ms": delay_ms, "offered_mbit": offered_mbit,
                    "real": real, "simulator": sim,
                    "relative_error": {
                        "throughput": rel_error(sim["throughput_mbit"], real["throughput_mbit"]),
                        "latency": rel_error(sim["latency_ms"], real["latency_ms"]),
                    },
                }
            )
            print(json.dumps(comparisons[-1], indent=2), flush=True)
    finally:
        net.stop()

    throughput_errors = [c["relative_error"]["throughput"] for c in comparisons if c["relative_error"]["throughput"] is not None]
    latency_errors = [c["relative_error"]["latency"] for c in comparisons if c["relative_error"]["latency"] is not None]

    report = {
        "scenarios": comparisons,
        "summary": {
            "n_scenarios": len(comparisons),
            "mean_throughput_relative_error": sum(throughput_errors) / len(throughput_errors) if throughput_errors else None,
            "max_throughput_relative_error": max(throughput_errors) if throughput_errors else None,
            "mean_latency_relative_error": sum(latency_errors) / len(latency_errors) if latency_errors else None,
            "max_latency_relative_error": max(latency_errors) if latency_errors else None,
        },
        "data_source": "emulated_mininet_vs_synthetic_simulator",
    }

    tables_dir = REPO_ROOT / "results" / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    with open(tables_dir / "simulator_calibration.json", "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
