"""B1 baseline: static shortest-path routing, for the Phase 22 comparison
study. Computes the single shortest path (by hop count, via
nx.shortest_path) once per flow and never reroutes for degradation or
congestion -- only recomputes when the currently-installed path is no
longer physically valid in the topology graph (a real link failure),
exactly the way ordinary L3 shortest-path routing would react. No ML, no
prediction, no persistence, no proactive avoidance. Represents "no
self-healing at all" as the comparison study's floor.
"""
from __future__ import annotations

import networkx as nx


def compute_shortest_path(graph: nx.Graph, src: int, dst: int) -> list[int] | None:
    try:
        return nx.shortest_path(graph, src, dst)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


class ShortestPathBaseline:
    """Tracks one path per flow; only recomputes when that path is no
    longer valid in the CURRENT topology graph, never in response to a
    degradation/congestion signal (this baseline has no such signal)."""

    def __init__(self):
        self._active_paths: dict[str, list[int]] = {}

    def get_or_compute_path(self, flow_key: str, graph: nx.Graph, src: int, dst: int) -> list[int] | None:
        current = self._active_paths.get(flow_key)
        if current is not None and self._path_still_valid(current, graph):
            return current

        new_path = compute_shortest_path(graph, src, dst)
        if new_path is not None:
            self._active_paths[flow_key] = new_path
        else:
            self._active_paths.pop(flow_key, None)
        return new_path

    @staticmethod
    def _path_still_valid(path: list[int], graph: nx.Graph) -> bool:
        return all(graph.has_edge(u, v) for u, v in zip(path, path[1:]))
