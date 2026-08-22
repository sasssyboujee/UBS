"""Kan Chiong Delivery Driver — time-dependent shortest path solver.

Correctness notes
-----------------
* Edges are bidirectional with the same base duration in both directions.
* An obstruction applies to one direction only (edge_id + from -> to) while
  its window ``[start_time, end_time)`` is active.
* ``speed_factor`` scales speed.  Traversal time for a full edge is
  ``base_duration_sec / speed_factor``; ``0.0`` blocks the directed traversal.
* No waiting at nodes.  A route may cycle on edges to burn time.
* If an obstruction becomes active *during* a traversal, only the remaining
  untravelled portion is affected.  For ``speed_factor == 0`` this means the
  driver waits **on the edge** until the blocking window ends.  Entering an
  edge while it is already blocked is impossible (that would mean waiting at
  the node), so such a departure yields no arrival.

Search
------
Dijkstra over ``(node, arrival_time)`` states.  We deliberately do **not**
apply per-regime dominance pruning: because waiting at nodes is forbidden, a
later arrival at the same node can be strictly better than an earlier one
(e.g. the earlier one would have to enter an edge that is currently blocked).
The only safe dominance used is after the last obstruction has ended — from
that point on the graph is static, so the earliest arrival at each node
dominates all later ones.
"""

from __future__ import annotations

import heapq
import time as _time
from bisect import bisect_right
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

EPS = 1e-6
INF = float("inf")

UNREACHABLE: dict[str, Any] = {
    "total_duration_sec": None,
    "arrival_time": None,
    "path": [],
}


def parse_iso8601(timestamp: str) -> float:
    """ISO-8601 timestamp -> epoch seconds (UTC).

    ``Z`` is the common suffix in the challenge; offsets are supported through
    ``datetime.fromisoformat``.  Naive timestamps are interpreted as UTC.
    """
    value = timestamp.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def format_iso8601(timestamp: float) -> str:
    """Epoch seconds -> ISO-8601 with ``Z`` suffix.

    Whole seconds are rendered without a fractional part (matching the
    examples); fractional seconds keep microsecond precision.
    """
    dt = datetime.fromtimestamp(timestamp, tz=UTC)
    if dt.microsecond == 0:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.isoformat().replace("+00:00", "Z")


def format_duration(seconds: float) -> int | float:
    """Return an int when the duration is integral, else a 6-dp float."""
    if abs(seconds - round(seconds)) < EPS:
        return round(seconds)
    return round(seconds, 6)


def build_segments(
    windows: list[tuple[float, float, float]],
) -> tuple[list[float], list[float], list[float]]:
    """Merge possibly-overlapping obstruction windows into a piecewise-constant
    speed-factor timeline spanning ``[-inf, inf]``.

    Returns ``(starts, ends, factors)`` where ``factors[i]`` applies on
    ``[starts[i], ends[i])``.  Overlapping windows take the most restrictive
    (smallest) factor; outside every window the factor is 1.
    """
    if not windows:
        return [-INF], [INF], [1.0]

    points = sorted({p for s, e, _ in windows for p in (s, e)})
    starts: list[float] = [-INF]
    ends: list[float] = [points[0]]
    factors: list[float] = [1.0]

    for a, b in pairwise(points):
        mid = (a + b) / 2.0
        factor = INF
        for s, e, f in windows:
            if s <= mid < e and f < factor:
                factor = f
        if factor == INF:
            factor = 1.0
        if factors[-1] == factor and ends[-1] == a:
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
    depart_time: float,
) -> float | None:
    """Arrival time when entering this directed edge at ``depart_time``.

    Returns ``None`` when the edge is blocked at the departure instant (which
    would force waiting at the node — not allowed).  If a blocking window
    starts mid-traversal the driver waits on the edge until it clears.
    """
    if duration == 0:
        idx = bisect_right(starts, depart_time) - 1
        idx = max(idx, 0)
        if factors[idx] == 0.0:
            return None
        return depart_time

    idx = bisect_right(starts, depart_time) - 1
    idx = max(idx, 0)

    # Blocked before entering the edge: cannot start the traversal.
    if factors[idx] == 0.0:
        return None

    remaining = 1.0  # fraction of the edge left to travel
    cur = depart_time
    n_segments = len(starts)

    while idx < n_segments:
        factor = factors[idx]
        end = ends[idx]
        cur = max(cur, starts[idx])

        if factor == 0.0:
            # Blocked mid-traversal: wait on the edge until this window ends.
            cur = end
            idx += 1
            continue

        time_needed = remaining * duration / factor
        if end == INF or cur + time_needed <= end:
            return cur + time_needed

        remaining -= factor * (end - cur) / duration
        cur = end
        idx += 1

    return None


def _solve(req: dict[str, Any], deadline: float | None = None) -> dict[str, Any]:
    """Solve a single Kan Chiong case.

    ``deadline`` is an optional ``time.monotonic()`` timestamp; when exceeded a
    :class:`TimeoutError` is raised so the batch router can cut its losses.
    """
    start_coord = (
        int(req["start_coordinate"][0]),
        int(req["start_coordinate"][1]),
    )
    end_coord = (
        int(req["end_coordinate"][0]),
        int(req["end_coordinate"][1]),
    )
    t0 = parse_iso8601(req["start_time"])

    # ---------------------------------------------------------------- nodes
    coord_to_idx: dict[tuple[int, int], int] = {}
    for node in req.get("nodes", []):
        coord = (int(node[0]), int(node[1]))
        if coord not in coord_to_idx:
            coord_to_idx[coord] = len(coord_to_idx)
    for coord in (start_coord, end_coord):
        if coord not in coord_to_idx:
            coord_to_idx[coord] = len(coord_to_idx)

    # Be tolerant if the ``nodes`` list is incomplete: edge endpoints define
    # nodes too (the spec always sends ``nodes``, but this never hurts).
    for edge in req.get("edges", []):
        for key in ("node1", "node2"):
            coord = (int(edge[key][0]), int(edge[key][1]))
            if coord not in coord_to_idx:
                coord_to_idx[coord] = len(coord_to_idx)

    n_nodes = len(coord_to_idx)
    start_idx = coord_to_idx[start_coord]
    end_idx = coord_to_idx[end_coord]

    if start_idx == end_idx:
        return {
            "total_duration_sec": 0,
            "arrival_time": format_iso8601(t0),
            "path": [],
        }

    # ---------------------------------------------------------------- arcs
    arcs: list[tuple[int, int, str, float]] = []
    arcs_by_node: list[list[int]] = [[] for _ in range(n_nodes)]

    for edge in req.get("edges", []):
        u = coord_to_idx[(int(edge["node1"][0]), int(edge["node1"][1]))]
        v = coord_to_idx[(int(edge["node2"][0]), int(edge["node2"][1]))]
        eid = edge["edge_id"]
        duration = float(edge["base_duration_sec"])

        idx = len(arcs)
        arcs.append((u, v, eid, duration))
        arcs_by_node[u].append(idx)

        idx = len(arcs)
        arcs.append((v, u, eid, duration))
        arcs_by_node[v].append(idx)

    # ------------------------------------------------- obstruction timelines
    arc_map: dict[tuple[str, int, int], int] = {}
    for arc_idx, (u, v, eid, _) in enumerate(arcs):
        arc_map[(eid, u, v)] = arc_idx

    windows: list[list[tuple[float, float, float]]] = [[] for _ in arcs]
    max_obs_end = t0

    for obs in req.get("obstructions", []):
        from_coord = (
            int(obs["edge"]["from"][0]),
            int(obs["edge"]["from"][1]),
        )
        to_coord = (
            int(obs["edge"]["to"][0]),
            int(obs["edge"]["to"][1]),
        )
        from_idx = coord_to_idx.get(from_coord)
        to_idx = coord_to_idx.get(to_coord)
        if from_idx is None or to_idx is None:
            continue

        arc_idx = arc_map.get((obs["edge_id"], from_idx, to_idx))
        if arc_idx is None:
            continue

        start = parse_iso8601(obs["start_time"])
        end = parse_iso8601(obs["end_time"])
        if end <= start:
            continue
        factor = float(obs["speed_factor"])
        windows[arc_idx].append((start, end, factor))
        max_obs_end = max(max_obs_end, end)

    seg_starts: list[list[float]] = []
    seg_ends: list[list[float]] = []
    seg_factors: list[list[float]] = []
    for arc_windows in windows:
        if arc_windows:
            arc_windows.sort(key=lambda w: (w[0], w[1]))
        starts, ends, factors = build_segments(arc_windows)
        seg_starts.append(starts)
        seg_ends.append(ends)
        seg_factors.append(factors)

    # ------------------------------------------------- static connectivity
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

    # ------------------------------------------------------------- search
    heap: list[tuple[float, int, int]] = []
    parent: dict[tuple[int, float], tuple[int | None, float | None, str]] = {}
    expanded: set[tuple[int, float]] = set()
    finalized: set[int] = set()

    counter = 0

    def push(arrival: float, node: int, prev_node: int | None, prev_time: float | None, eid: str) -> None:
        nonlocal counter
        key = (node, arrival)
        if key in parent:
            return
        parent[key] = (prev_node, prev_time, eid)
        counter += 1
        heapq.heappush(heap, (arrival, counter, node))

    push(t0, start_idx, None, None, "")

    while heap:
        if deadline is not None and _time.monotonic() > deadline:
            raise TimeoutError

        t, _, u = heapq.heappop(heap)

        if u == end_idx:
            duration = t - t0
            path: list[str] = []
            state = (u, t)
            while state in parent:
                prev_node, prev_time, eid = parent[state]
                if prev_node is None:
                    break
                path.append(eid)
                state = (prev_node, prev_time)  # type: ignore[assignment]
            path.reverse()
            return {
                "total_duration_sec": format_duration(duration),
                "arrival_time": format_iso8601(t),
                "path": path,
            }

        if u in finalized:
            continue

        state = (u, t)
        if state in expanded:
            continue
        expanded.add(state)

        # After the final obstruction ends the network is static; the earliest
        # arrival at a node dominates every later one.
        if t >= max_obs_end:
            finalized.add(u)

        for arc_idx in arcs_by_node[u]:
            v = arcs[arc_idx][1]
            if v in finalized:
                continue
            arrival = travel_time(
                seg_starts[arc_idx],
                seg_ends[arc_idx],
                seg_factors[arc_idx],
                arcs[arc_idx][3],
                t,
            )
            if arrival is None:
                continue
            new_state = (v, arrival)
            if new_state in expanded:
                continue
            push(arrival, v, u, t, arcs[arc_idx][2])

    return UNREACHABLE


def solve_case(case: dict, deadline: float | None = None) -> dict:
    """Solve one case, never raising (malformed input yields UNREACHABLE)."""
    try:
        return _solve(case, deadline)
    except TimeoutError:
        return UNREACHABLE
    except Exception:  # noqa: BLE001 - a malformed case must not crash the batch
        return UNREACHABLE
