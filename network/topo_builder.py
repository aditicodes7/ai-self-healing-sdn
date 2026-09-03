#!/usr/bin/env python3
"""Generic Mininet topology builder driven by YAML topology definitions.

New scenarios are data (network/topologies/*.yaml), not code: this module
only knows how to translate the YAML schema (switches/hosts/links) into a
Mininet network, so an experiment adds a topology by adding a YAML file.

Must run under the VM's SYSTEM Python 3, with root (sudo) -- not venv-ctrl
or venv-ml. The apt `mininet` package installs against system Python, and
creating network namespaces needs root. See docs/decisions.md.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

# Mininet imports are deferred into the functions that need them, not
# module-level: this module's load_topology_spec()/TopologySpecError are
# also imported by controller/telemetry_collector.py (running under
# venv-ctrl, which has no mininet package at all -- mininet is only
# installed against the VM's system Python, see docs/decisions.md). A
# module-level `import mininet` would make this whole file unimportable
# from venv-ctrl.

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPO_DIR = REPO_ROOT / "network" / "topologies"


class TopologySpecError(ValueError):
    """Raised when a topology YAML file is missing or malformed."""


def load_topology_spec(name: str) -> dict:
    path = TOPO_DIR / f"{name}.yaml"
    if not path.exists():
        raise TopologySpecError(f"topology spec not found: {path}")

    with open(path) as f:
        spec = yaml.safe_load(f)

    for key in ("switches", "hosts", "links"):
        if key not in spec:
            raise TopologySpecError(f"topology spec '{name}' is missing required key '{key}'")

    switch_ids = {sw["id"] for sw in spec["switches"]}
    for host in spec["hosts"]:
        if host["switch"] not in switch_ids:
            raise TopologySpecError(
                f"host '{host['id']}' references unknown switch '{host['switch']}'"
            )
    for link in spec["links"]:
        for endpoint in link["endpoints"]:
            if endpoint not in switch_ids:
                raise TopologySpecError(
                    f"link {link['endpoints']} references unknown switch '{endpoint}'"
                )

    return spec


def build_network(
    spec: dict, controller_ip: str, controller_port: int, fail_mode: str = "standalone"
) -> Mininet:
    """fail_mode='standalone' (OVS's own local learning-switch fallback) lets
    topology/link sanity checks (Phase 3's pingall gate) pass with no SDN
    controller running at all. Once a real controller app exists (Phase 4
    onward), pass fail_mode='secure' so all forwarding is actually decided
    by the controller instead of OVS's local learning falling back
    silently."""
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSSwitch, RemoteController

    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController(
        "c0", controller=RemoteController, ip=controller_ip, port=controller_port
    )

    switches = {}
    for sw in spec["switches"]:
        switches[sw["id"]] = net.addSwitch(
            sw["id"], protocols="OpenFlow13", failMode=fail_mode
        )

    hosts = {}
    for h in spec["hosts"]:
        hosts[h["id"]] = net.addHost(h["id"], ip=h.get("ip"))
        net.addLink(hosts[h["id"]], switches[h["switch"]])

    for link in spec["links"]:
        a, b = link["endpoints"]
        link_opts = {k: link[k] for k in ("bw", "delay", "loss") if k in link}
        net.addLink(switches[a], switches[b], **link_opts)

    return net


def enable_rstp_and_wait(net: Mininet, switch_ids: list[str], settle_s: float = 15.0) -> None:
    """Enable RSTP on every switch bridge and wait for it to converge.

    Our topologies (network/topologies/*.yaml) have real L2 cycles by
    design (multiple redundant paths -- that is the point of this project).
    In OVS fail-mode='standalone' (used so topology/link sanity checks work
    with no SDN controller running, e.g. before Phase 4's controller app
    exists), a plain learning-switch fallback would flood broadcast/ARP
    traffic around those cycles and storm the network -- confirmed
    empirically (Phase 3: 100% dropped without this). RSTP blocks redundant
    links until the tree converges, then pingall works.

    Not used when fail_mode='secure': once a real SDN controller is
    installing exact-match flows (Phase 4 onward), there is no flooding to
    loop in the first place.
    """
    for sw_id in switch_ids:
        switch = net.get(sw_id)
        switch.cmd(f"ovs-vsctl set Bridge {sw_id} rstp_enable=true")
    time.sleep(settle_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topo", required=True,
        help="topology name (loads network/topologies/<name>.yaml)",
    )
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument(
        "--fail-mode", choices=["standalone", "secure"], default="standalone",
        help="OVS fail-mode: 'standalone' (local learning-switch fallback, "
             "works with no SDN controller) or 'secure' (all forwarding "
             "decided by the controller). Default 'standalone' so this "
             "script's own pingall sanity check needs no controller.",
    )
    parser.add_argument(
        "--test", choices=["pingall"],
        help="run a test and exit instead of dropping into the Mininet CLI",
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    from mininet.log import setLogLevel

    setLogLevel(args.log_level)
    spec = load_topology_spec(args.topo)
    net = build_network(spec, args.controller_ip, args.controller_port, args.fail_mode)

    net.start()
    if args.fail_mode == "standalone":
        enable_rstp_and_wait(net, [sw["id"] for sw in spec["switches"]])

    exit_code = 0
    try:
        if args.test == "pingall":
            dropped_pct = net.pingAll()
            exit_code = 1 if dropped_pct > 0 else 0
        else:
            from mininet.cli import CLI

            CLI(net)
    finally:
        net.stop()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
