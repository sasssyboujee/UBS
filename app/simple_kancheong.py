import json
import heapq
import itertools
import math
from datetime import datetime, timezone
from typing import Any

EPS = 1e-9

UNREACHABLE: dict[str, Any] = {"total_duration_sec": None, "arrival_time": None, "path": []}


def parse_iso8601(timestamp: str) -> float:
    timestamp = timestamp.replace("Z", "+00:00")
    return datetime.fromisoformat(timestamp).timestamp()


def format_iso8601(timestamp: float) -> str:
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if abs(timestamp - round(timestamp)) < EPS:
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return dt.isoformat().replace("+00:00", "Z")


def _solve(req: dict) -> dict:
    start_coord = tuple(req["start_coordinate"])
    end_coord = tuple(req["end_coordinate"])
    start_time = parse_iso8601(req["start_time"])

    adjacency = {}
    for edge in req.get("edges", []):
        u, v = tuple(edge["node1"]), tuple(edge["node2"])
        eid, duration = edge["edge_id"], edge["base_duration_sec"]
        adjacency.setdefault(u, []).append((v, eid, duration))
        adjacency.setdefault(v, []).append((u, eid, duration))

    obstruction_map = {}
    max_obs_end = start_time
    for obs in req.get("obstructions", []):
        eid = obs["edge_id"]
        u, v = tuple(obs["edge"]["from"]), tuple(obs["edge"]["to"])
        os_, oe_ = parse_iso8601(obs["start_time"]), parse_iso8601(obs["end_time"])
        sf = obs["speed_factor"]
        obstruction_map.setdefault((eid, u, v), []).append((os_, oe_, sf))
        max_obs_end = max(max_obs_end, oe_)

    threshold = max_obs_end

    def build_output(duration: float, path: list[str]) -> dict:
        if abs(duration - round(duration)) < EPS:
            duration_out: Any = round(duration)
        else:
            duration_out = round(duration, 6)
        return {
            "total_duration_sec": duration_out,
            "arrival_time": format_iso8601(start_time + duration_out),
            "path": path,
        }

    if start_coord == end_coord:
        return build_output(0, [])

    def travel_time(u, v, eid, base_duration, depart_time):
        intervals = obstruction_map.get((eid, u, v), [])

        if base_duration == 0:
            for (os_, oe_, sf) in intervals:
                if os_ <= depart_time < oe_ and sf == 0.0:
                    return None
            return depart_time

        t = depart_time
        remaining = float(base_duration)

        while remaining > EPS:
            active_speeds = []
            next_boundary = math.inf
            for (os_, oe_, sf) in intervals:
                if os_ <= t < oe_:
                    active_speeds.append(sf)
                    next_boundary = min(next_boundary, oe_)
                elif t < os_:
                    next_boundary = min(next_boundary, os_)

            speed = min(active_speeds) if active_speeds else 1.0
            if speed == 0.0:
                return None

            if next_boundary == math.inf:
                t += remaining / speed
                remaining = 0.0
                continue

            window = next_boundary - t
            coverable = window * speed
            if remaining <= coverable + EPS:
                t += remaining / speed
                remaining = 0.0
            else:
                remaining -= coverable
                t = next_boundary

        return t

    counter = itertools.count()
    heap = [(float(start_time), next(counter), start_coord)]
    expanded_states = set()
    finalized_nodes = set()
    came_from = {}

    while heap:
        t, _, u = heapq.heappop(heap)

        if u == end_coord:
            duration = t - start_time
            path = []
            state = (u, t)
            while state in came_from:
                prev_node, prev_time, eid = came_from[state]
                path.append(eid)
                state = (prev_node, prev_time)
            path.reverse()
            return build_output(duration, path)

        if u in finalized_nodes:
            continue

        state = (u, t)
        if state in expanded_states:
            continue
        expanded_states.add(state)

        if t >= threshold:
            finalized_nodes.add(u)

        for (v, eid, base_duration) in adjacency.get(u, []):
            if v in finalized_nodes:
                continue
            arrival = travel_time(u, v, eid, base_duration, t)
            if arrival is None:
                continue
            new_state = (v, arrival)
            if new_state in expanded_states:
                continue
            if new_state not in came_from:
                came_from[new_state] = (u, t, eid)
            heapq.heappush(heap, (arrival, next(counter), v))

    return UNREACHABLE


def solve_case(case: dict) -> dict:
    try:
        return _solve(case)
    except Exception:  # noqa: BLE001 - malformed input must not crash the batch
        return UNREACHABLE