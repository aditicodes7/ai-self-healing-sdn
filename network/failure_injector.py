#!/usr/bin/env python3
"""Applies real Mininet/tc-level faults to specific links and writes a
ground-truth JSON-lines record of every injection. Phase 9's dataset
generator reads this ground truth to LABEL windows (a link is
"degraded/failed" within label.horizon_s of a fault's start) -- labels
never come from anywhere else.

Runs under the VM's SYSTEM Python 3 alongside topo_builder.py and
traffic_generator.py -- same reasoning (the apt `mininet` package targets
system Python, and Mininet needs root), see docs/decisions.md.

Six real fault types, each a genuine Linux tc/OVS-level manipulation of
the live link -- never a fabricated telemetry value written directly to
the database:
  congestion          -- bw reduced to a fraction of configured capacity
  gradual_degradation -- bw/delay ramped linearly across sub-steps
  packet_loss         -- tc netem loss% injected
  high_latency        -- tc netem delay increased
  rising_errors       -- tc netem corrupt% injected (the kernel counts
                          corrupted frames as rx/tx errors -- real counter
                          movement telemetry_collector.py will observe,
                          not a fabricated errors value)
  link_failure        -- link brought down entirely (net.configLinkStatus)
Recovery restores the link's original YAML-configured bw/delay/loss (or
brings it back up) and is logged as its own ground-truth event, separate
from the 6 fault types above.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FAULT_TYPES = (
    "congestion",
    "gradual_degradation",
    "packet_loss",
    "high_latency",
    "rising_errors",
    "link_failure",
)


class FailureInjector:
    def __init__(self, net, topology_spec: dict, ground_truth_path: str | Path):
        self.net = net
        self._original_link_params = {
            tuple(sorted(link["endpoints"])): {
                "bw": link.get("bw"),
                "delay": link.get("delay"),
                "loss": link.get("loss", 0),
            }
            for link in topology_spec["links"]
        }
        self.ground_truth_path = Path(ground_truth_path)
        self.ground_truth_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_link(self, sw_a: str, sw_b: str):
        matches = self.net.linksBetween(self.net.get(sw_a), self.net.get(sw_b))
        if not matches:
            raise ValueError(f"no link between {sw_a} and {sw_b}")
        return matches[0]

    def _write_ground_truth(self, record: dict) -> None:
        record = {"logged_at": time.time(), **record}
        with open(self.ground_truth_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def _apply_instant_fault(self, link, fault_type: str, params: dict) -> dict:
        sw_a_intf, sw_b_intf = link.intf1, link.intf2

        if fault_type == "congestion":
            original_bw = self._original_bw(link)
            bw_fraction = params.get("bw_fraction", 0.2)
            new_bw = original_bw * bw_fraction
            sw_a_intf.config(bw=new_bw)
            sw_b_intf.config(bw=new_bw)
            return {"bw_fraction": bw_fraction, "new_bw_mbit": new_bw}

        if fault_type == "packet_loss":
            loss_pct = params.get("loss_pct", 10)
            sw_a_intf.config(loss=loss_pct)
            sw_b_intf.config(loss=loss_pct)
            return {"loss_pct": loss_pct}

        if fault_type == "high_latency":
            delay_ms = params.get("delay_ms", 50)
            sw_a_intf.config(delay=f"{delay_ms}ms")
            sw_b_intf.config(delay=f"{delay_ms}ms")
            return {"delay_ms": delay_ms}

        if fault_type == "rising_errors":
            corrupt_pct = params.get("corrupt_pct", 5)
            for intf in (sw_a_intf, sw_b_intf):
                intf.cmd(f"tc qdisc change dev {intf.name} root netem corrupt {corrupt_pct}%")
            return {"corrupt_pct": corrupt_pct}

        if fault_type == "link_failure":
            self.net.configLinkStatus(link.intf1.node.name, link.intf2.node.name, "down")
            return {}

        raise ValueError(f"{fault_type!r} is not an instant fault")

    def _apply_gradual_degradation(self, link, duration_s: float, params: dict) -> dict:
        steps = params.get("steps", 5)
        step_duration = duration_s / steps
        original_bw = self._original_bw(link)
        target_bw_fraction = params.get("target_bw_fraction", 0.15)
        target_delay_ms = params.get("target_delay_ms", 40)

        for step in range(1, steps + 1):
            frac = step / steps
            bw = original_bw * (1 - frac * (1 - target_bw_fraction))
            delay_ms = frac * target_delay_ms
            link.intf1.config(bw=bw, delay=f"{delay_ms:.1f}ms")
            link.intf2.config(bw=bw, delay=f"{delay_ms:.1f}ms")
            time.sleep(step_duration)

        return {
            "steps": steps,
            "target_bw_fraction": target_bw_fraction,
            "target_delay_ms": target_delay_ms,
        }

    def _original_bw(self, link) -> float:
        endpoints = tuple(sorted((link.intf1.node.name, link.intf2.node.name)))
        return self._original_link_params[endpoints]["bw"] or 10.0

    def inject(self, sw_a: str, sw_b: str, fault_type: str, duration_s: float, **params) -> None:
        if fault_type not in FAULT_TYPES:
            raise ValueError(f"unknown fault_type {fault_type!r}, must be one of {FAULT_TYPES}")

        link_id = "-".join(sorted((sw_a, sw_b)))
        link = self._get_link(sw_a, sw_b)
        start_ts = time.time()

        if fault_type == "gradual_degradation":
            applied = self._apply_gradual_degradation(link, duration_s, params)
            elapsed_s = time.time() - start_ts
        else:
            applied = self._apply_instant_fault(link, fault_type, params)
            elapsed_s = 0.0

        self._write_ground_truth(
            {
                "event": "fault_start",
                "link_id": link_id,
                "fault_type": fault_type,
                "start_ts": start_ts,
                "duration_s": duration_s,
                "params": applied,
            }
        )

        remaining_s = duration_s - elapsed_s
        if remaining_s > 0:
            time.sleep(remaining_s)

        self._write_ground_truth(
            {"event": "fault_end", "link_id": link_id, "fault_type": fault_type, "end_ts": time.time()}
        )
        self.recover(sw_a, sw_b, fault_type=fault_type)

    def recover(self, sw_a: str, sw_b: str, fault_type: str | None = None) -> None:
        link_id = "-".join(sorted((sw_a, sw_b)))
        original = self._original_link_params[tuple(sorted((sw_a, sw_b)))]

        if fault_type == "link_failure":
            self.net.configLinkStatus(sw_a, sw_b, "up")
        else:
            link = self._get_link(sw_a, sw_b)
            link.intf1.config(bw=original["bw"], delay=original["delay"], loss=original["loss"] or 0)
            link.intf2.config(bw=original["bw"], delay=original["delay"], loss=original["loss"] or 0)
            if fault_type == "rising_errors":
                for intf in (link.intf1, link.intf2):
                    intf.cmd(f"tc qdisc change dev {intf.name} root netem corrupt 0%")

        self._write_ground_truth({"event": "recovery", "link_id": link_id, "recovered_at": time.time()})


def main(argv: list[str] | None = None) -> int:
    from mininet.log import setLogLevel

    from network.topo_builder import build_network, enable_rstp_and_wait, load_topology_spec

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topo", required=True)
    parser.add_argument("--link", required=True, nargs=2, metavar=("SW_A", "SW_B"))
    parser.add_argument("--fault-type", required=True, choices=FAULT_TYPES)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--ground-truth", default=str(REPO_ROOT / "results" / "raw" / "ground_truth.jsonl"))
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--fail-mode", choices=["standalone", "secure"], default="secure")
    args = parser.parse_args(argv)

    setLogLevel("info")
    spec = load_topology_spec(args.topo)
    net = build_network(spec, args.controller_ip, args.controller_port, args.fail_mode)
    net.start()
    if args.fail_mode == "standalone":
        enable_rstp_and_wait(net, [sw["id"] for sw in spec["switches"]])

    try:
        injector = FailureInjector(net, spec, args.ground_truth)
        injector.inject(args.link[0], args.link[1], args.fault_type, args.duration)
    finally:
        net.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
