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
  gradual_degradation -- bw/delay ramped to a worse steady state, held
  packet_loss         -- tc netem loss% injected
  high_latency        -- tc netem delay increased
  rising_errors       -- tc netem corrupt% injected (the kernel counts
                          corrupted frames as rx/tx errors -- real counter
                          movement telemetry_collector.py will observe,
                          not a fabricated errors value)
  link_failure        -- link brought down entirely (net.configLinkStatus)

PRE-FAULT RAMP (added 2026-09-02, see docs/limitations.md's Phase 9
finding): every fault type now ramps from healthy toward its target
intensity over `ramp_in_s` seconds BEFORE `fault_start` is logged, instead
of jumping there instantly. Phase 9's ML training found near-chance
performance and traced it to exactly this: with an instantaneous
step-function fault, the label's anticipation window (the label.horizon_s
seconds before a fault) is statistically indistinguishable from any
healthy window, because nothing in the network has actually changed yet.
The ramp gives the anticipation window a genuine, gradually-worsening
signal to learn from -- modeling the (realistic) idea that real
degradation is often preceded by a worsening trend, not a switch flip.
`fault_start` in the ground truth still marks the moment full fault
intensity is reached (unchanged semantics for ml/dataset/label_rules.py);
the ramp happens entirely before that timestamp.

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

DEFAULT_RAMP_IN_S = 12.0  # > label.horizon_s (10s default) so the whole
                          # anticipation window sees the ramp in progress
RAMP_STEPS = 4


def _parse_delay_ms(delay_str) -> float:
    if not delay_str:
        return 0.0
    return float(str(delay_str).replace("ms", "").strip())


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

    def _original_bw(self, link) -> float:
        endpoints = tuple(sorted((link.intf1.node.name, link.intf2.node.name)))
        return self._original_link_params[endpoints]["bw"] or 10.0

    def _ramp_targets_for(self, fault_type: str, params: dict, original_bw: float, original_delay_ms: float):
        """(target_bw, target_delay_ms, target_loss_pct, target_corrupt_pct)
        for the given fault type -- what the link should look like once the
        pre-fault ramp completes and the fault is at full intensity."""
        target_bw, target_delay_ms = original_bw, original_delay_ms
        target_loss_pct, target_corrupt_pct = 0.0, 0.0

        if fault_type == "congestion":
            target_bw = original_bw * params.get("bw_fraction", 0.2)
        elif fault_type == "packet_loss":
            target_loss_pct = params.get("loss_pct", 10)
        elif fault_type == "high_latency":
            target_delay_ms = params.get("delay_ms", 50)
        elif fault_type == "rising_errors":
            target_corrupt_pct = params.get("corrupt_pct", 5)
        elif fault_type == "link_failure":
            # Real links often show signs of trouble before going down
            # entirely -- ramp toward a degraded-but-up state, then the
            # instant hard failure happens on top of that at fault_start.
            target_bw = original_bw * params.get("pre_failure_bw_fraction", 0.3)
            target_loss_pct = params.get("pre_failure_loss_pct", 15)
        elif fault_type == "gradual_degradation":
            target_bw = original_bw * params.get("target_bw_fraction", 0.15)
            target_delay_ms = params.get("target_delay_ms", 40)

        return target_bw, target_delay_ms, target_loss_pct, target_corrupt_pct

    def _ramp_to_target(
        self, link, target_bw: float, target_delay_ms: float, target_loss_pct: float,
        target_corrupt_pct: float, ramp_in_s: float, steps: int,
    ) -> None:
        if ramp_in_s <= 0 or steps <= 0:
            return
        original_bw = self._original_bw(link)
        endpoints = tuple(sorted((link.intf1.node.name, link.intf2.node.name)))
        original = self._original_link_params[endpoints]
        original_delay_ms = _parse_delay_ms(original["delay"])
        original_loss_pct = original["loss"] or 0

        step_duration = ramp_in_s / steps
        for step in range(1, steps + 1):
            frac = step / steps
            bw = original_bw + frac * (target_bw - original_bw)
            delay_ms = original_delay_ms + frac * (target_delay_ms - original_delay_ms)
            loss_pct = original_loss_pct + frac * (target_loss_pct - original_loss_pct)
            link.intf1.config(bw=bw, delay=f"{delay_ms:.2f}ms", loss=loss_pct)
            link.intf2.config(bw=bw, delay=f"{delay_ms:.2f}ms", loss=loss_pct)
            if target_corrupt_pct > 0:
                corrupt_now = frac * target_corrupt_pct
                for intf in (link.intf1, link.intf2):
                    intf.cmd(f"tc qdisc change dev {intf.name} root netem corrupt {corrupt_now:.2f}%")
            time.sleep(step_duration)

    def _apply_full_intensity(self, link, fault_type: str, params: dict, targets: tuple) -> dict:
        """Snap the link to exactly its target values (the ramp should
        already be close, but floating-point step accumulation shouldn't
        be what "full intensity" means) and perform any fault-specific
        action the ramp itself can't express (bringing the link down)."""
        target_bw, target_delay_ms, target_loss_pct, target_corrupt_pct = targets
        sw_a_intf, sw_b_intf = link.intf1, link.intf2

        if fault_type == "link_failure":
            self.net.configLinkStatus(link.intf1.node.name, link.intf2.node.name, "down")
            return {"pre_failure_bw_mbit": target_bw, "pre_failure_loss_pct": target_loss_pct}

        sw_a_intf.config(bw=target_bw, delay=f"{target_delay_ms:.2f}ms", loss=target_loss_pct)
        sw_b_intf.config(bw=target_bw, delay=f"{target_delay_ms:.2f}ms", loss=target_loss_pct)
        if target_corrupt_pct > 0:
            for intf in (sw_a_intf, sw_b_intf):
                intf.cmd(f"tc qdisc change dev {intf.name} root netem corrupt {target_corrupt_pct}%")

        applied = {"bw_mbit": target_bw, "delay_ms": target_delay_ms}
        if fault_type == "packet_loss":
            applied = {"loss_pct": target_loss_pct}
        elif fault_type == "high_latency":
            applied = {"delay_ms": target_delay_ms}
        elif fault_type == "rising_errors":
            applied = {"corrupt_pct": target_corrupt_pct}
        elif fault_type == "congestion":
            applied = {"bw_fraction": params.get("bw_fraction", 0.2), "new_bw_mbit": target_bw}
        elif fault_type == "gradual_degradation":
            applied = {"target_bw_fraction": params.get("target_bw_fraction", 0.15), "target_delay_ms": target_delay_ms}
        return applied

    def inject(
        self, sw_a: str, sw_b: str, fault_type: str, duration_s: float,
        ramp_in_s: float = DEFAULT_RAMP_IN_S, ramp_steps: int = RAMP_STEPS, **params,
    ) -> None:
        if fault_type not in FAULT_TYPES:
            raise ValueError(f"unknown fault_type {fault_type!r}, must be one of {FAULT_TYPES}")

        link_id = "-".join(sorted((sw_a, sw_b)))
        link = self._get_link(sw_a, sw_b)
        original_bw = self._original_bw(link)
        endpoints = tuple(sorted((sw_a, sw_b)))
        original_delay_ms = _parse_delay_ms(self._original_link_params[endpoints]["delay"])

        targets = self._ramp_targets_for(fault_type, params, original_bw, original_delay_ms)
        self._ramp_to_target(link, *targets, ramp_in_s=ramp_in_s, steps=ramp_steps)

        start_ts = time.time()
        applied = self._apply_full_intensity(link, fault_type, params, targets)
        applied["ramp_in_s"] = ramp_in_s

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

        time.sleep(duration_s)

        self._write_ground_truth(
            {"event": "fault_end", "link_id": link_id, "fault_type": fault_type, "end_ts": time.time()}
        )
        self.recover(sw_a, sw_b, fault_type=fault_type)

    def recover(self, sw_a: str, sw_b: str, fault_type: str | None = None) -> None:
        link_id = "-".join(sorted((sw_a, sw_b)))
        original = self._original_link_params[tuple(sorted((sw_a, sw_b)))]

        if fault_type == "link_failure":
            self.net.configLinkStatus(sw_a, sw_b, "up")

        link = self._get_link(sw_a, sw_b)
        link.intf1.config(bw=original["bw"], delay=original["delay"], loss=original["loss"] or 0)
        link.intf2.config(bw=original["bw"], delay=original["delay"], loss=original["loss"] or 0)
        if fault_type in ("rising_errors", "link_failure"):
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
    parser.add_argument("--ramp-in", type=float, default=DEFAULT_RAMP_IN_S)
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
        injector.inject(args.link[0], args.link[1], args.fault_type, args.duration, ramp_in_s=args.ramp_in)
    finally:
        net.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
