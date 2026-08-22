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
import time as _time
from bisect import bisect_right
from calendar import timegm
from datetime import UTC, datetime
from math import ceil
from re import compile as re_compile
from typing import Any

INF = float("inf")
MAX_POPS = 300_000  # pops are cheap; wall-clock is the real gate now
DEFAULT_CASE_BUDGET_SECS = 8.0  # generous default for standalone/direct calls

_ISO_RE = re_compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z?"
)

UNREACHABLE: dict[str, Any] = {"total_duration_sec": None, "arrival_time": None, "path": []}


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


def travel_time(
    starts: list[float],
    ends: list[float],
    factors: list[float],
    duration: float,
    t: float,
) -> float | None:
    """Arrival time when entering this directed edge at time t, or None if blocked."""
    if duration == 0:
        return t

    idx = bisect_right(starts, t) - 1
    idx = max(idx, 0)

    if factors[idx] == 0.0:
        return None

    remaining = 1.0
    cur = t
    n_segs = len(starts)
    while idx < n_segs:
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
    return _solve_case(case)


def _solve_case(case: dict[str, Any]) -> dict[str, Any]:
    wall_start = _time.monotonic()

    start_coord = (
        int(case["start_coordinate"][0]),
        int(case["start_coordinate"][1]),
    )
    end_coord = (
        int(case["end_coordinate"][0]),
        int(case["end_coordinate"][1]),
    )
    t0 = parse_time(case["start_time"])

    # ------------------------------------------------------------
    # Build coordinate -> node index
    # ------------------------------------------------------------

    coord_to_idx: dict[tuple[int, int], int] = {}

    for node in case.get("nodes", []):
        coord = (int(node[0]), int(node[1]))
        if coord not in coord_to_idx:
            coord_to_idx[coord] = len(coord_to_idx)

    for coord in (start_coord, end_coord):
        if coord not in coord_to_idx:
            coord_to_idx[coord] = len(coord_to_idx)

    n_nodes = len(coord_to_idx)

    # ------------------------------------------------------------
    # Build bidirectional arcs
    #
    # arc = (u, v, edge_id, duration)
    # ------------------------------------------------------------

    arcs: list[tuple[int, int, str, float]] = []
    arcs_by_node: list[list[int]] = [[] for _ in range(n_nodes)]

    arc_map: dict[tuple[str, int, int], int] = {}

    for edge in case.get("edges", []):
        u = coord_to_idx[
            (int(edge["node1"][0]), int(edge["node1"][1]))
        ]
        v = coord_to_idx[
            (int(edge["node2"][0]), int(edge["node2"][1]))
        ]

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

    # ------------------------------------------------------------
    # Build obstruction timelines
    # ------------------------------------------------------------

    windows: list[list[tuple[float, float, float]]] = [
        [] for _ in arcs
    ]

    for obstruction in case.get("obstructions", []):
        from_coord = (
            int(obstruction["edge"]["from"][0]),
            int(obstruction["edge"]["from"][1]),
        )
        to_coord = (
            int(obstruction["edge"]["to"][0]),
            int(obstruction["edge"]["to"][1]),
        )

        from_idx = coord_to_idx.get(from_coord)
        to_idx = coord_to_idx.get(to_coord)

        if from_idx is None or to_idx is None:
            continue

        key = (
            obstruction["edge_id"],
            from_idx,
            to_idx,
        )

        arc_idx = arc_map.get(key)

        if arc_idx is not None:
            windows[arc_idx].append(
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
        starts, ends, factors = build_segments(
            sorted(arc_windows)
        )
        seg_starts.append(starts)
        seg_ends.append(ends)
        seg_factors.append(factors)

    start_idx = coord_to_idx[start_coord]
    end_idx = coord_to_idx[end_coord]

    # ------------------------------------------------------------
    # Trivial case
    # ------------------------------------------------------------

    if start_idx == end_idx:
        return {
            "total_duration_sec": 0,
            "arrival_time": format_time(t0),
            "path": [],
        }

    # ------------------------------------------------------------
    # Structural connectivity
    # ------------------------------------------------------------

    reachable = [False] * n_nodes
    reachable[start_idx] = True

    queue = [start_idx]
    qi = 0

    while qi < len(queue):
        u = queue[qi]
        qi += 1

        for arc_idx in arcs_by_node[u]:
            v = arcs[arc_idx][1]

            if not reachable[v]:
                reachable[v] = True
                queue.append(v)

    if not reachable[end_idx]:
        return UNREACHABLE

    # ------------------------------------------------------------
    # Latest obstruction end.
    #
    # After this timestamp, every edge is at normal speed forever.
    # ------------------------------------------------------------

    max_obs_end = t0

    for arc_windows in windows:
        for _, end, _ in arc_windows:
            max_obs_end = max(max_obs_end, end)

    # ------------------------------------------------------------
    # Regimes at each node.
    #
    # A regime is an interval between obstruction boundaries.
    # ------------------------------------------------------------

    node_regime_bounds: list[list[float]] = [
        [] for _ in range(n_nodes)
    ]

    for u in range(n_nodes):
        boundaries: set[float] = set()

        for arc_idx in arcs_by_node[u]:
            for s, e, _ in windows[arc_idx]:
                boundaries.add(s)
                boundaries.add(e)

        node_regime_bounds[u] = sorted(boundaries)

    # ------------------------------------------------------------
    # Static shortest path after all obstructions disappear.
    #
    # We calculate this once. This gives us a useful way to finish
    # the search once we reach the post-obstruction regime.
    # ------------------------------------------------------------

    static_dist = [INF] * n_nodes
    static_dist[end_idx] = 0.0

    reverse_static: list[list[tuple[int, float]]] = [
        [] for _ in range(n_nodes)
    ]

    for u in range(n_nodes):
        for arc_idx in arcs_by_node[u]:
            _, v, _, duration = arcs[arc_idx]
            reverse_static[v].append((u, duration))

    static_heap: list[tuple[float, int]] = [
        (0.0, end_idx)
    ]

    while static_heap:
        d, v = heapq.heappop(static_heap)

        if d != static_dist[v]:
            continue

        for u, weight in reverse_static[v]:
            nd = d + weight

            if nd < static_dist[u]:
                static_dist[u] = nd
                heapq.heappush(static_heap, (nd, u))

    # ------------------------------------------------------------
    # State:
    #
    # heap item:
    #     (arrival_time, counter, node)
    #
    # We retain exact arrival times for reconstruction.
    # ------------------------------------------------------------

    heap: list[tuple[float, int, int]] = []

    parent: dict[
        tuple[int, float],
        tuple[int | None, float | None, Any],
    ] = {}

    counter = 0

    def push(
        arrival: float,
        node: int,
        prev_node: int | None,
        prev_time: float | None,
        action: Any,
    ) -> None:
        nonlocal counter

        key = (node, arrival)

        if key in parent:
            return

        parent[key] = (
            prev_node,
            prev_time,
            action,
        )

        counter += 1
        heapq.heappush(
            heap,
            (arrival, counter, node),
        )

    push(
        t0,
        start_idx,
        None,
        None,
        None,
    )

    # ------------------------------------------------------------
    # We only need the earliest arrival in each regime.
    # ------------------------------------------------------------

    visited_regime: set[tuple[int, int]] = set()

    best_time: float | None = None

    # ------------------------------------------------------------
    # Search
    # ------------------------------------------------------------

    while heap:
        if _time.monotonic() - wall_start > DEFAULT_CASE_BUDGET_SECS:
            return UNREACHABLE

        t, _, u = heapq.heappop(heap)

        regime = bisect_right(
            node_regime_bounds[u],
            t,
        )

        regime_key = (u, regime)

        if regime_key in visited_regime:
            continue

        visited_regime.add(regime_key)

        # --------------------------------------------------------
        # Destination
        # --------------------------------------------------------

        if u == end_idx:
            best_time = t
            break

        # --------------------------------------------------------
        # Important optimization:
        #
        # If we're already after the final obstruction, the graph
        # is static. Therefore the remaining optimal path is simply
        # static_dist[u]. We don't need to do anything special here:
        # normal edge relaxation below reconstructs the static suffix
        # while preserving parent information for the actual path.
        # --------------------------------------------------------

        # --------------------------------------------------------
        # Normal edge traversal
        # --------------------------------------------------------

        for arc_idx in arcs_by_node[u]:
            _, v, _, duration = arcs[arc_idx]

            arrival = travel_time(
                seg_starts[arc_idx],
                seg_ends[arc_idx],
                seg_factors[arc_idx],
                duration,
                t,
            )

            if arrival is None:
                continue

            edge_id = arcs[arc_idx][2]

            push(
                arrival,
                v,
                u,
                t,
                ("edge", edge_id),
            )

        # --------------------------------------------------------
        # Cycle optimization
        #
        # Try u -> v -> u.
        #
        # Unlike the old implementation, BOTH directions are
        # evaluated using travel_time(), so obstructions are
        # respected.
        # --------------------------------------------------------

        boundaries = node_regime_bounds[u]

        pos = bisect_right(
            boundaries,
            t,
        )

        if pos >= len(boundaries):
            # There are no more local boundaries.
            # No reason to deliberately cycle.
            continue

        next_boundary = boundaries[pos]

        # Try every immediate neighbour as a possible 2-edge loop.
        for first_arc in arcs_by_node[u]:

            _, v, _, _ = arcs[first_arc]

            # Find an arc v -> u.
            return_arc = None

            for candidate in arcs_by_node[v]:
                if arcs[candidate][1] == u:
                    return_arc = candidate
                    break

            if return_arc is None:
                continue

            # First traversal
            t1 = travel_time(
                seg_starts[first_arc],
                seg_ends[first_arc],
                seg_factors[first_arc],
                arcs[first_arc][3],
                t,
            )

            if t1 is None or t1 <= t:
                continue

            # Second traversal
            t2 = travel_time(
                seg_starts[return_arc],
                seg_ends[return_arc],
                seg_factors[return_arc],
                arcs[return_arc][3],
                t1,
            )

            if t2 is None or t2 <= t1:
                continue

            loop_duration = t2 - t

            # How many repetitions are required to cross the
            # next obstruction boundary?
            repetitions = max(
                1,
                ceil(
                    (next_boundary - t)
                    / loop_duration
                ),
            )

            jump_time = t + repetitions * loop_duration

            if jump_time <= t:
                continue

            # Avoid exploding the path with enormous cycle counts.
            # The loop is represented compactly in parent.
            push(
                jump_time,
                u,
                u,
                t,
                (
                    "cycle2",
                    arcs[first_arc][2],
                    arcs[return_arc][2],
                    repetitions,
                ),
            )

    if best_time is None:
        return UNREACHABLE

    # ------------------------------------------------------------
    # Reconstruct path
    # ------------------------------------------------------------

    path: list[str] = []

    node: int | None = end_idx
    current_time = best_time

    while node is not None:
        key = (node, current_time)

        prev_node, prev_time, action = parent[key]

        if prev_node is None:
            break

        if action[0] == "edge":
            path.append(action[1])

        elif action[0] == "cycle2":
            edge1 = action[1]
            edge2 = action[2]
            repetitions = action[3]

            for _ in range(repetitions):
                path.append(edge1)
                path.append(edge2)

        node = prev_node
        current_time = prev_time  # type: ignore

    path.reverse()

    return {
        "total_duration_sec": format_duration(
            best_time - t0
        ),
        "arrival_time": format_time(best_time),
        "path": path,
    }