"""Single point of contact with the SDN controller framework.

Every other module under controller/ imports from here, never directly from
ryu or os_ken — this is what lets the whole controller run unmodified on
whichever backend the Phase 2 GO/NO-GO smoke test selects (see
docs/decisions.md and PROJECT_STATE.md for which one that turned out to be).
Enforced by tests/unit/test_of_compat.py.
"""
try:
    from ryu.base import app_manager
    from ryu.controller import ofp_event
    from ryu.controller.handler import (
        CONFIG_DISPATCHER,
        DEAD_DISPATCHER,
        MAIN_DISPATCHER,
        set_ev_cls,
    )
    from ryu.ofproto import ofproto_v1_3
    from ryu.lib.packet import packet, ethernet, ether_types
    from ryu.lib import hub
    from ryu.topology import event as topo_event
    from ryu.topology.api import get_switch, get_link
    BACKEND = "ryu"
except ImportError:
    from os_ken.base import app_manager
    from os_ken.controller import ofp_event
    from os_ken.controller.handler import (
        CONFIG_DISPATCHER,
        DEAD_DISPATCHER,
        MAIN_DISPATCHER,
        set_ev_cls,
    )
    from os_ken.ofproto import ofproto_v1_3
    from os_ken.lib.packet import packet, ethernet, ether_types
    from os_ken.lib import hub
    from os_ken.topology import event as topo_event
    from os_ken.topology.api import get_switch, get_link
    BACKEND = "os-ken"

__all__ = [
    "app_manager",
    "ofp_event",
    "CONFIG_DISPATCHER",
    "DEAD_DISPATCHER",
    "MAIN_DISPATCHER",
    "set_ev_cls",
    "ofproto_v1_3",
    "packet",
    "ethernet",
    "ether_types",
    "hub",
    "topo_event",
    "get_switch",
    "get_link",
    "BACKEND",
]
