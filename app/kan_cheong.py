"""Solver for the Kan Chiong Delivery Driver challenge.

Time-dependent shortest path over a graph whose edges have a base duration and
directional, time-windowed obstructions that scale the traversal speed.

Key rules implemented here:

- Edges are bidirectional with the same base duration in both directions.
- An obstruction applies only to one direction (edge_id + from -> to) while
  its window [start_time, end_time) is active.
- speed_factor scales speed: traversal takes base_duration / speed_factor.
  A factor of 0.0 blocks the directed traversal.
- You may not wait at nodes, so a route may cycle on edges to burn time.
- If an obstruction becomes active mid-traversal, only the remaining
  untraveled portion is affected (piecewise speed integration).

The search is a Dijkstra over (node, arrival_time) states. To keep long
"wait by cycling" routes tractable, states may also jump forward in time by
repeating a loop on an unobstructed incident edge.
"""

from __future__ import annotations

import heapq
import itertools
from bisect import bisect_right
from calendar import timegm
from datetime import UTC, datetime
from math import ceil
from re import compile as re_compile
from typing import Any

INF = float("inf")
MAX_POPS = 50_000

_ISO_RE = re_compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z?"
)


def parse_time(value: str) -> float:
    """ISO-8601 (with Z or offset) to epoch seconds — fast path for 'Z' suffix."""
    m = _ISO_RE.match(value)
    if m:
        y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        frac = float(f"0.{m.group(7)}") if m.group(7) else 0.0
        return timegm((y, mo, d, h, mi, s, 0, 0, 0)) + frac
    return datetime.fromisoformat(value).timestamp()


def format_time(ts: float) -> str:
    """Epoch seconds to ISO-8601 with Z suffix."""
    dt = datetime.fromtimestamp(ts, tz=UTC)
    if dt.microsecond == 0:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    base = dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".")
    return f"{base}Z"


def format_duration(seconds: float) -> int | float:
    if abs(seconds - round(seconds)) < 1e-9:
        return round(seconds)
    return round(seconds, 6)


def build_segments(windows: list[tuple[float, float, float]]) -> tuple[list[float], list[float], list[float]]:
    """Piecewise-constant speed-factor timeline for one directed edge.

    Returns (starts, ends, factors): parallel lists where factor applies on
    [starts[i], ends[i]). The timeline spans [-inf, inf] with factor 1 outside
    obstruction windows; overlapping windows take the most restrictive factor.
    """
    if not windows:
        return [-INF], [INF], [1.0]

    points = sorted({point for s, e, _ in windows for point in (s, e)})
    starts: list[float] = [-INF]
    ends: list[float] = [points[0]]
    factors: list[float] = [1.0]

    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        mid = (a + b) / 2.0
        factor = 1.0
        for s, e, fac in windows:
            if s <= mid < e and fac < factor:
                factor = fac
        if factors and factors[-1] == factor and ends[-1] == a:
            ends[-1] = b
        else:
            starts.append(a)
            ends.append(b)
            factors.append(factor)

    starts.append(points[-1])
    ends.append(INF)
    factors.append(1.0)
    return starts, ends, factors


def factor_at(starts: list[float], factors: list[float], t: float) -> float:
    idx = bisect_right(starts, t) - 1
    idx = max(idx, 0)
    return factors[idx]


def travel_time(
    starts: list[float],
    ends: list[float],
    factors: list[float],
    duration: float,
    t: float,
) -> float | None:
    """Arrival time when entering this directed edge at time t, or None if blocked."""
    idx = bisect_right(starts, t) - 1
    idx = max(idx, 0)

    if factors[idx] == 0.0:
        return None

    remaining = 1.0
    cur = t
    while idx < len(starts):
        factor = factors[idx]
        end = ends[idx]
        cur = max(cur, starts[idx])
        if factor == 0.0:
            # Blocked mid-traversal: no progress until this window ends.
            cur = end
            idx += 1
            continue
        time_to_finish = remaining * duration / factor
        if end == INF or cur + time_to_finish <= end:
            return cur + time_to_finish
        remaining -= factor * (end - cur) / duration
        cur = end
        idx += 1
    return None


def solve_case(case: dict[str, Any]) -> dict[str, Any]:
    try:
        return _solve_case(case)
    except Exception:  # noqa: BLE001 - a malformed case must not fail the whole batch
        return {"total_duration_sec": None, "arrival_time": None, "path": []}


def _solve_case(case: dict[str, Any]) -> dict[str, Any]:
    start_coord = (int(case["start_coordinate"][0]), int(case["start_coordinate"][1]))
    end_coord = (int(case["end_coordinate"][0]), int(case["end_coordinate"][1]))
    t0 = parse_time(case["start_time"])

    coord_to_idx: dict[tuple[int, int], int] = {}
    for node in case.get("nodes", []):
        coord = (int(node[0]), int(node[1]))
        if coord not in coord_to_idx:
            coord_to_idx[coord] = len(coord_to_idx)
    for coord in (start_coord, end_coord):
        if coord not in coord_to_idx:
            coord_to_idx[coord] = len(coord_to_idx)
    n_nodes = len(coord_to_idx)

    # Directed arcs: [u, v, edge_id, base_duration]
    arcs: list[tuple[int, int, str, float]] = []
    arcs_by_node: list[list[int]] = [[] for _ in range(n_nodes)]
    arc_map: dict[tuple[str, int, int], int] = {}

    for edge in case.get("edges", []):
        u = coord_to_idx[(int(edge["node1"][0]), int(edge["node1"][1]))]
        v = coord_to_idx[(int(edge["node2"][0]), int(edge["node2"][1]))]
        edge_id = edge["edge_id"]
        duration = float(edge["base_duration_sec"])

        idx = len(arcs)
        arcs.append((u, v, edge_id, duration))
        arcs_by_node[u].append(idx)
        arc_map[(edge_id, u, v)] = idx

        idx = len(arcs)
        arcs.append((v, u, edge_id, duration))
        arcs_by_node[v].append(idx)
        arc_map[(edge_id, v, u)] = idx

    windows: list[list[tuple[float, float, float]]] = [[] for _ in range(len(arcs))]
    for obstruction in case.get("obstructions", []):
        from_coord = tuple(obstruction["edge"]["from"])
        to_coord = tuple(obstruction["edge"]["to"])
        from_idx = coord_to_idx.get((int(from_coord[0]), int(from_coord[1])))
        to_idx = coord_to_idx.get((int(to_coord[0]), int(to_coord[1])))
        if from_idx is None or to_idx is None:
            continue
        key = (obstruction["edge_id"], from_idx, to_idx)
        if key in arc_map:
            windows[arc_map[key]].append(
                (
                    parse_time(obstruction["start_time"]),
                    parse_time(obstruction["end_time"]),
                    float(obstruction["speed_factor"]),
                )
            )

    seg_starts: list[list[float]] = []
    seg_ends: list[list[float]] = []
    seg_factors: list[list[float]] = []
    for arc_windows in windows:
        starts, ends, factors = build_segments(sorted(arc_windows))
        seg_starts.append(starts)
        seg_ends.append(ends)
        seg_factors.append(factors)

    start_idx = coord_to_idx[start_coord]
    end_idx = coord_to_idx[end_coord]

    if start_idx == end_idx:
        return {
            "total_duration_sec": 0,
            "arrival_time": format_time(t0),
            "path": [],
        }

    # Quick structural connectivity check (ignoring obstructions).
    reachable = [False] * n_nodes
    reachable[start_idx] = True
    q = [start_idx]
    qi = 0
    while qi < len(q):
        node = q[qi]; qi += 1
        for arc in arcs_by_node[node]:
            v = arcs[arc][1]
            if not reachable[v]:
                reachable[v] = True
                q.append(v)
    if not reachable[end_idx]:
        return {"total_duration_sec": None, "arrival_time": None, "path": []}

    # Free waiting loops: unobstructed incident edges can be cycled to burn time.
    free_loops: list[list[tuple[str, int]]] = [[] for _ in range(n_nodes)]
    for idx, (u, v, edge_id, duration) in enumerate(arcs):
        if duration > 0 and not windows[idx]:
            traversals = 1 if u == v else 2
            free_loops[u].append((edge_id, traversals, int(duration) * traversals))

    node_boundaries: list[list[float]] = []
    for u in range(n_nodes):
        boundaries = {
            point
            for arc in arcs_by_node[u]
            for s, e, _ in windows[arc]
            for point in (s, e)
        }
        node_boundaries.append(sorted(boundaries))

    parent: dict[tuple[int, float], tuple[int | None, float | None, Any]] = {}
    heap: list[tuple[float, int, int]] = []
    counter = itertools.count()

    def push(time: float, node: int, prev_node: int | None, prev_time: float | None, action: Any) -> None:
        key = (node, time)
        if key not in parent:
            parent[key] = (prev_node, prev_time, action)
        heapq.heappush(heap, (time, next(counter), node))

    push(t0, start_idx, None, None, None)
    visited: set[tuple[int, float]] = set()
    best_time: float | None = None
    pops = 0

    while heap:
        t, _, u = heapq.heappop(heap)
        key = (u, t)
        if key in visited:
            continue
        visited.add(key)
        pops += 1

        # Safety valve for temporally unreachable destinations: without a
        # destination state the time-expanded graph is infinite.
        if pops > MAX_POPS:
            return {"total_duration_sec": None, "arrival_time": None, "path": []}

        if u == end_idx:
            best_time = t
            break

        # Normal edge traversals.
        for arc in arcs_by_node[u]:
            arrival = travel_time(
                seg_starts[arc], seg_ends[arc], seg_factors[arc], arcs[arc][3], t
            )
            if arrival is None:
                continue
            push(arrival, arcs[arc][1], u, t, ("edge", arcs[arc][2]))

        # "Waiting" by cycling on unobstructed loops until the next time the
        # environment at this node changes (an outgoing obstruction boundary).
        boundaries = node_boundaries[u]
        pos = bisect_right(boundaries, t)
        if pos < len(boundaries):
            next_boundary = boundaries[pos]
            for edge_id, traversals, loop_duration in free_loops[u]:
                loops = ceil((next_boundary - t) / loop_duration)
                loops = max(loops, 1)
                jump_time = t + loops * loop_duration
                if jump_time > t:
                    push(jump_time, u, u, t, ("loop", edge_id, loops * traversals))

    if best_time is None:
        return {"total_duration_sec": None, "arrival_time": None, "path": []}

    path: list[str] = []
    node: int | None = end_idx
    t = best_time
    while node is not None:
        prev_node, prev_time, action = parent[(node, t)]
        if prev_node is None:
            break
        if action[0] == "edge":
            path.append(action[1])
        else:
            path.extend([action[1]] * action[2])
        node, t = prev_node, prev_time if prev_time is not None else t0

    path.reverse()
    return {
        "total_duration_sec": format_duration(best_time - t0),
        "arrival_time": format_time(best_time),
        "path": path,
    }
