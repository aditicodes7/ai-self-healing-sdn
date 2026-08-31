"""Shared domain vocabulary imported by both venvs (common/ is pure Python).

These enums are the canonical string values written into SQLite columns
(links.state, incidents.status, verification_results.classification, ...).
Keeping them here instead of duplicating magic strings across
controller/, ml/, rl/, backend/, and experiments/ is what keeps the DB
contents self-consistent across the two-interpreter split.
"""
from __future__ import annotations

import enum


class LinkHealthState(str, enum.Enum):
    HEALTHY = "HEALTHY"
    AT_RISK = "AT_RISK"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class IncidentStatus(str, enum.Enum):
    OPEN = "OPEN"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNRESOLVED = "UNRESOLVED"


class VerificationClassification(str, enum.Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class RouteSelector(str, enum.Enum):
    """Which decision-making system chose a path change, for baseline comparisons."""

    B1_SHORTEST_PATH = "B1_SHORTEST_PATH"
    B2_RULE_BASED = "B2_RULE_BASED"
    P1_ML_HEURISTIC = "P1_ML_HEURISTIC"
    P2_ML_RL = "P2_ML_RL"


class DataSource(str, enum.Enum):
    """Rule 4 (honest data labelling): every generated dataset/run carries one
    of these. Never call either "real-world traffic"."""

    EMULATED_MININET = "emulated_mininet"
    SYNTHETIC_SIMULATOR = "synthetic_simulator"


class FlowRuleResult(str, enum.Enum):
    INSTALLED = "INSTALLED"
    ERROR = "ERROR"


# EtherType used by controller/telemetry_collector.py's latency probe
# frames (0x9999 is in the unassigned/experimental range per IEEE 802;
# never seen on real host traffic, so main_app.py's L2Forwarding can
# reliably ignore these frames the same way it ignores LLDP).
PROBE_ETH_TYPE = 0x9999
