#!/usr/bin/env python3
"""iperf3-driven traffic generation between Mininet hosts.

Two ways to use this module:
  1. As a library: dataset generation (Phase 8) and the experiment runner
     (Phase 22) import start_iperf_server()/run_iperf_client() and drive
     them against Mininet host objects from an already-built network.
  2. As a script: `python3 network/traffic_generator.py --topo scenario_a
     --client h1 --server h6 --duration 10` builds the named topology,
     runs one client->server iperf3 flow, prints the parsed JSON summary,
     and tears the network down.

Must run under the VM's SYSTEM Python 3 with root, same as topo_builder.py
-- see docs/decisions.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from network.topo_builder import build_network, enable_rstp_and_wait, load_topology_spec  # noqa: E402


class IperfError(RuntimeError):
    """Raised when an iperf3 client run fails or produces no parseable output."""


def start_iperf_server(host, port: int = 5201):
    """Start `iperf3 -s` on a Mininet host, returns the Popen handle.

    Caller is responsible for stopping it (stop_iperf_server) once no more
    clients will connect.
    """
    return host.popen(["iperf3", "-s", "-p", str(port), "-J"])


def stop_iperf_server(proc) -> None:
    proc.terminate()
    proc.wait()


def run_iperf_client(
    client_host,
    server_ip: str,
    port: int = 5201,
    duration_s: int = 10,
    protocol: str = "tcp",
    bandwidth: str | None = None,
) -> dict[str, Any]:
    """Run `iperf3 -c` on client_host against server_ip, return the parsed
    JSON summary (iperf3's own -J output; real measured numbers, not
    derived/estimated by us)."""
    cmd = ["iperf3", "-c", server_ip, "-p", str(port), "-t", str(duration_s), "-J"]
    if protocol == "udp":
        cmd.append("-u")
        if bandwidth:
            cmd += ["-b", bandwidth]

    output = client_host.cmd(" ".join(cmd))
    try:
        # Mininet's Node.cmd() merges stderr into stdout, so a warning
        # iperf3 prints after its JSON blob (e.g. on a connection hiccup)
        # would otherwise break a plain json.loads(). raw_decode() parses
        # just the JSON object and ignores anything appended after it.
        obj, _ = json.JSONDecoder().raw_decode(output)
        return obj
    except json.JSONDecodeError as exc:
        raise IperfError(f"iperf3 client produced no parseable JSON: {output!r}") from exc


def summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Pull out the fields this project actually reports (throughput,
    retransmits for TCP; jitter/loss for UDP), from iperf3's own `end`
    summary block. Never fabricates a number iperf3 didn't report."""
    if "error" in result:
        raise IperfError(f"iperf3 reported an error: {result['error']}")

    end = result.get("end", {})
    summary: dict[str, Any] = {}

    if "sum_received" in end:
        summary["throughput_bps"] = end["sum_received"].get("bits_per_second")
    elif "sum" in end:
        summary["throughput_bps"] = end["sum"].get("bits_per_second")
        summary["jitter_ms"] = end["sum"].get("jitter_ms")
        summary["lost_percent"] = end["sum"].get("lost_percent")

    if "sum_sent" in end:
        summary["retransmits"] = end["sum_sent"].get("retransmits")

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topo", required=True)
    parser.add_argument("--client", required=True, help="host id, e.g. h1")
    parser.add_argument("--server", required=True, help="host id, e.g. h6")
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--protocol", choices=["tcp", "udp"], default="tcp")
    parser.add_argument("--port", type=int, default=5201)
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--fail-mode", choices=["standalone", "secure"], default="standalone")
    args = parser.parse_args(argv)

    from mininet.log import setLogLevel

    setLogLevel("info")

    spec = load_topology_spec(args.topo)
    net = build_network(spec, args.controller_ip, args.controller_port, args.fail_mode)
    net.start()
    if args.fail_mode == "standalone":
        enable_rstp_and_wait(net, [sw["id"] for sw in spec["switches"]])
    try:
        client_host = net.get(args.client)
        server_host = net.get(args.server)

        server_proc = start_iperf_server(server_host, port=args.port)
        time.sleep(1)  # let the server bind before the client connects

        result = run_iperf_client(
            client_host,
            server_host.IP(),
            port=args.port,
            duration_s=args.duration,
            protocol=args.protocol,
        )
        stop_iperf_server(server_proc)

        summary = summarize_result(result)
        print(json.dumps(summary, indent=2))
    finally:
        net.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
