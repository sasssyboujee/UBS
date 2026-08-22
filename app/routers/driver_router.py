
from collections import defaultdict
from datetime import UTC, datetime
from heapq import heappop, heappush
from math import inf

from fastapi import APIRouter

router = APIRouter()


def parse_time(s: str) -> float:
    """ISO-8601 timestamp -> Unix seconds."""
    return datetime.fromisoformat(s).timestamp()


def iso_time(ts: float) -> str:
    """Unix seconds -> ISO-8601 UTC timestamp."""
    return (
        datetime.fromtimestamp(ts, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def coord_key(c):
    return (c[0], c[1])


def travel_time(
    edge,
    from_node,
    departure_time,
    obstruction_map,
):
    """
    Calculate the actual travel time for one directed traversal.

    Speed factor interpretation:
        1.0 = normal speed
        0.5 = half speed -> twice the duration
        2.0 = double speed -> half the duration
        0.0 = completely blocked

    Obstructions are directional.

    Important:
    If an obstruction begins while travelling, only the remaining
    distance is affected.
    """

    edge_id = edge["edge_id"]
    base_duration = edge["base_duration_sec"]

    # Zero-duration edges require special handling.
    if base_duration == 0:
        # A zero-length traversal takes no time.
        return 0.0

    direction = (
        coord_key(from_node),
        coord_key(
            edge["node2"]
            if coord_key(edge["node1"]) == coord_key(from_node)
            else edge["node1"]
        ),
    )

    # Get obstructions for this exact directed edge.
    obs_list = obstruction_map.get((edge_id, direction), [])

    if not obs_list:
        return float(base_duration)

    # We simulate the traversal in "distance units".
    #
    # Normal speed:
    #     1 distance unit / base_duration seconds
    #
    # Therefore we need to consume 1.0 unit of distance.
    remaining_distance = 1.0
    current_time = departure_time

    # There can be overlapping obstructions.
    # We handle all time boundaries chronologically.
    boundaries = set()

    for obs in obs_list:
        boundaries.add(obs["start"])
        boundaries.add(obs["end"])

    boundaries = sorted(
        t for t in boundaries
        if t >= current_time
    )

    # Include all intervals that might become relevant.
    # We repeatedly determine the active speed at current_time.
    while remaining_distance > 1e-12:

        active_factors = []

        for obs in obs_list:
            if obs["start"] <= current_time < obs["end"]:
                active_factors.append(obs["speed_factor"])

        if active_factors:
            # Multiple obstructions:
            # The slowest applicable speed wins.
            speed_factor = min(active_factors)
        else:
            speed_factor = 1.0

        # Completely blocked.
        if speed_factor == 0.0:
            # Find when the blocking obstruction(s) end.
            next_time = min(
                obs["end"]
                for obs in obs_list
                if obs["start"] <= current_time < obs["end"]
                and obs["speed_factor"] == 0.0
            )

            # No progress until then.
            current_time = next_time
            continue

        # Find the next obstruction boundary.
        next_boundary = inf

        for obs in obs_list:
            if obs["start"] > current_time:
                next_boundary = min(next_boundary, obs["start"])

            if obs["end"] > current_time and obs["start"] <= current_time:
                next_boundary = min(next_boundary, obs["end"])

        # Effective speed is proportional to speed_factor.
        #
        # base_duration seconds are required for 1.0 distance
        # at speed_factor = 1.
        #
        # Therefore:
        #   distance travelled = elapsed / base_duration * speed_factor
        #
        time_to_finish = remaining_distance * base_duration / speed_factor

        if current_time + time_to_finish <= next_boundary:
            current_time += time_to_finish
            remaining_distance = 0.0
        else:
            elapsed = next_boundary - current_time

            if elapsed < 0:
                elapsed = 0

            distance_covered = (
                elapsed * speed_factor / base_duration
            )

            remaining_distance -= distance_covered
            current_time = next_boundary

    return current_time - departure_time


def solve_case(case):
    start = coord_key(case["start_coordinate"])
    end = coord_key(case["end_coordinate"])
    start_time = parse_time(case["start_time"])

    # ------------------------------------------------------------------
    # Build edge lookup
    # ------------------------------------------------------------------

    edges = {}

    # adjacency[node] = list of:
    # {
    #     "edge": original edge,
    #     "to": destination coordinate
    # }
    adjacency = defaultdict(list)

    for edge in case["edges"]:
        edge_id = edge["edge_id"]
        node1 = coord_key(edge["node1"])
        node2 = coord_key(edge["node2"])

        edges[edge_id] = edge

        # Bidirectional.
        adjacency[node1].append({
            "edge": edge,
            "to": node2,
        })

        adjacency[node2].append({
            "edge": edge,
            "to": node1,
        })

    # ------------------------------------------------------------------
    # Build directional obstruction map
    # ------------------------------------------------------------------

    obstruction_map = defaultdict(list)

    for obs in case.get("obstructions", []):
        edge_id = obs["edge_id"]

        frm = coord_key(obs["edge"]["from"])
        to = coord_key(obs["edge"]["to"])

        obstruction_map[(edge_id, (frm, to))].append({
            "start": parse_time(obs["start_time"]),
            "end": parse_time(obs["end_time"]),
            "speed_factor": float(obs["speed_factor"]),
        })

    # Sort obstruction intervals.
    for key in obstruction_map:
        obstruction_map[key].sort(
            key=lambda x: (x["start"], x["end"])
        )

    # ------------------------------------------------------------------
    # Special case
    # ------------------------------------------------------------------

    if start == end:
        return {
            "total_duration_sec": 0,
            "arrival_time": iso_time(start_time),
            "path": [],
        }

    # ------------------------------------------------------------------
    # Time-dependent search
    #
    # A normal Dijkstra implementation is insufficient because arriving
    # at the same node later can be useful when the outgoing edge was
    # blocked at the earlier arrival time.
    #
    # labels[node] contains non-dominated arrival times.
    # ------------------------------------------------------------------

    # Each state:
    # (arrival_time, counter, node)
    #
    # counter gives deterministic heap ordering when times are equal.
    heap = []
    counter = 0

    heappush(heap, (start_time, counter, start))

    # labels[node] = list of known useful arrival times.
    #
    # We keep this relatively small using dominance:
    # if an earlier arrival can reach everything that a later arrival
    # can reach, the later one can be discarded.
    labels = defaultdict(list)
    labels[start].append(start_time)

    # Parent information for path reconstruction.
    #
    # parent[state_time_key] = (
    #     previous_node,
    #     previous_arrival_time,
    #     edge_id
    # )
    #
    # Floating point timestamps are problematic as dictionary keys,
    # so we use the integer microsecond representation.
    def time_key(t):
        return round(t * 1_000_000)

    parent = {}

    best_destination = None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    while heap:
        current_time, _, node = heappop(heap)

        current_key = time_key(current_time)

        # Check whether this state is still present as a useful label.
        if not any(time_key(t) == current_key for t in labels[node]):
            continue

        if node == end:
            best_destination = current_time
            break

        for item in adjacency[node]:
            edge = item["edge"]
            next_node = item["to"]

            duration = travel_time(
                edge=edge,
                from_node=node,
                departure_time=current_time,
                obstruction_map=obstruction_map,
            )

            if duration == inf:
                continue

            next_time = current_time + duration

            # ----------------------------------------------------------
            # Dominance check
            #
            # A label t1 dominates t2 if:
            #
            #     t1 <= t2
            #
            # We cannot blindly discard later labels in the presence of
            # blocking windows, so we only use this conservative check
            # when the later state is exactly reproducible / redundant.
            #
            # The search additionally limits duplicate timestamps.
            # ----------------------------------------------------------

            next_key = time_key(next_time)

            duplicate = False
            for existing in labels[next_node]:
                if time_key(existing) == next_key:
                    duplicate = True
                    break

            if duplicate:
                continue

            # ----------------------------------------------------------
            # Keep the new label.
            #
            # To prevent pathological explosion from useless cycles,
            # discard a new label if there is an earlier label and both
            # are outside a useful obstruction transition.
            #
            # We retain multiple labels because the problem explicitly
            # allows cycling to escape future blocking windows.
            # ----------------------------------------------------------

            labels[next_node].append(next_time)

            parent[(next_node, next_key)] = (
                node,
                current_key,
                edge["edge_id"],
            )

            counter += 1
            heappush(
                heap,
                (next_time, counter, next_node),
            )

    # ------------------------------------------------------------------
    # No route
    # ------------------------------------------------------------------

    if best_destination is None:
        return {
            "total_duration_sec": None,
            "arrival_time": None,
            "path": [],
        }

    # ------------------------------------------------------------------
    # Reconstruct path
    # ------------------------------------------------------------------

    path = []

    node = end
    current_key = time_key(best_destination)

    while node != start:
        state = (node, current_key)

        if state not in parent:
            # Should not happen, but protects against malformed state.
            return {
                "total_duration_sec": None,
                "arrival_time": None,
                "path": [],
            }

        previous_node, previous_time_key, edge_id = parent[state]

        path.append(edge_id)

        node = previous_node
        current_key = previous_time_key

    path.reverse()

    total_duration = best_destination - start_time

    # Durations are specified as integer seconds, but time-dependent
    # traversal can still produce fractional values if speed factors
    # result in them. The expected API uses seconds, so preserve an
    # integer when numerically integral.
    if abs(total_duration - round(total_duration)) < 1e-9:
        total_duration = int(round(total_duration))
    else:
        total_duration = round(total_duration, 6)

    return {
        "total_duration_sec": total_duration,
        "arrival_time": iso_time(best_destination),
        "path": path,
    }
@router.post("/kan-cheong-delivery-driver")
async def kan_cheong_delivery_driver(batch: dict):
    return {
        case_id: solve_case(case)
        for case_id, case in batch.items()
    }