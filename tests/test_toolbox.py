"""Offline regression tests for the Tool-box RAG recall and phase-3 city tools."""

import pytest

from app import mcp_server2 as tb

# ----------------------------------------------------------------------
# recall (RAG)
# ----------------------------------------------------------------------

FACT_AIR = (
    "Two unrelated incidents were logged in close succession this past autumn. "
    "An oxygen scrubber failure occurred on 2 November, prompting an emergency "
    "ventilation drill that cleared the affected module within eleven minutes."
)

FACT_DRIVERS = (
    "The authority employs sixty-eight certified line drivers across its five "
    "color-coded lines. That figure excludes depot mechanics, dispatchers, and "
    "administrative staff."
)

FACT_DRYER = (
    "The board's resolution establishing the cooperative's shared drier rota was "
    "formally adopted on 21 May, following a unanimous vote of the seven sitting "
    "board members present that evening."
)


def _materials(*docs):
    return [{"title": f"doc{i + 1}", "text": text} for i, text in enumerate(docs)]


def test_recall_paraphrase_air_scrubbing():
    distractor = (
        "The Cobalt Line stretches thirty-four point two kilometers from its "
        "northern terminus to its southern depot loop."
    )
    chunks = tb.recall("On what date did the air-scrubbing equipment break down?", _materials(distractor, FACT_AIR))
    assert chunks, "expected at least one passage"
    assert "2 November" in " ".join(chunks)


def test_recall_paraphrase_licensed_motormen():
    distractor = (
        "Ridership has grown steadily since the network's last major expansion, "
        "and the authority publishes quarterly ridership figures."
    )
    chunks = tb.recall(
        "Roughly how many licensed motormen operate service across the network?",
        _materials(distractor, FACT_DRIVERS),
    )
    assert "sixty-eight" in " ".join(chunks)


def test_recall_paraphrase_drying_machine():
    distractor = (
        "The shared potato harvester rotates between member holdings every eleven "
        "days during peak harvest weeks."
    )
    chunks = tb.recall(
        "When did the board formally approve the arrangement for sharing the drying machine?",
        _materials(distractor, FACT_DRYER),
    )
    assert "21 May" in " ".join(chunks)


def test_recall_stays_under_token_budget():
    filler = " ".join(
        f"Paragraph {i} about the station grid and its many mundane details." for i in range(120)
    )
    chunks = tb.recall("When was the sensor grid last brought back into alignment?", _materials(filler))
    assert chunks
    total = sum(len(tb._ENCODING.encode(c)) for c in chunks)
    assert total <= tb.TOKEN_BUDGET


def test_recall_includes_runner_up_document():
    doc_a = (
        "The library opened in 1987 near the east gate.\n\n"
        "Students use the reading room every evening."
    )
    doc_b = (
        "The library is on the east side of campus.\n\n"
        "It closes at ten on Fridays."
    )
    chunks = tb.recall("Where is the library?", _materials(doc_a, doc_b))
    joined = " ".join(chunks)
    assert "east side of campus" in joined
    assert "east gate" in joined  # runner-up doc's top passage carried too


def test_recall_requires_question():
    with pytest.raises(tb.ToolboxError):
        tb.recall("")


# ----------------------------------------------------------------------
# navigate / route
# ----------------------------------------------------------------------

GRAPH = {
    "adjacency": {"A": {"B": 4.0, "C": 2.0}, "B": {"D": 3.0}, "C": {"D": 2.0}},
    "tolls": {"A": 5.0, "B": 1.0, "C": 9.0, "D": 2.0},
}


def test_navigate_least_cost():
    path, cost = tb._find_path("m", "A", "D", None, [], "", GRAPH)
    assert path == ["B", "D"]
    assert cost == 4.0 + 1.0 + 3.0 + 2.0


def test_navigate_respects_visited():
    path, _ = tb._find_path("m", "A", "D", None, ["B"], "", GRAPH)
    assert path == ["C", "D"]


def test_navigate_hop_limit():
    path, _ = tb._find_path("m", "A", "D", 1, [], "", GRAPH)
    assert path is None  # needs 2 hops


# ----------------------------------------------------------------------
# phase 3: venues
# ----------------------------------------------------------------------

VENUES = {
    "day": "Monday",
    "venues": [
        {"name": "Nine Quarters", "x": 7, "y": 3, "available": [["20:00", "23:00"]]},
        {"name": "Loam", "x": 9, "y": 0, "available": [["11:00", "15:00"]]},
        {"name": "Tallow Green", "x": 0, "y": 9, "available": [["08:00", "13:00"]]},
    ],
}


def test_venues_open_basic():
    assert tb.venues_open("Monday", "08:00", venues=VENUES) == ["Tallow Green"]
    assert tb.venues_open("Monday", "13:00", venues=VENUES) == ["Loam"]
    assert tb.venues_open("Monday", "20:00", venues=VENUES) == ["Nine Quarters"]
    assert tb.venues_open("Monday", "23:00", venues=VENUES) == []


def test_venues_open_tool_returns_joined_names(client):
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "find_open_venues",
                "arguments": {"day": "Monday", "time": "13:00", "venues": VENUES},
            },
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == "Loam"


# ----------------------------------------------------------------------
# phase 3: meeting windows
# ----------------------------------------------------------------------


def test_window_clean_beats_earlier_tentative():
    window = tb.find_meeting_window(
        "Tuesday",
        "12:00",
        "14:00",
        duration_minutes=60,
        people=["ada"],
        busy=[["13:00", "14:00"]],
        tentative=[["12:00", "13:00"]],
        schedules={"ada": {"busy": []}},
    )
    assert window == "13:00-14:00"


def test_window_tentative_gives_way_when_nothing_clean():
    window = tb.find_meeting_window(
        "Tuesday",
        "12:00",
        "14:00",
        duration_minutes=60,
        people=["ada"],
        busy=[["13:00", "14:00"]],
        tentative=[["12:00", "13:00"]],
        schedules={"ada": {"busy": []}},
    )
    assert window == "13:00-14:00"


def test_window_friend_busy_blocks():
    window = tb.find_meeting_window(
        "Tuesday",
        "13:00",
        "18:00",
        duration_minutes=60,
        people=["ada"],
        schedules={"ada": {"busy": [["13:00", "14:00"]]}},
    )
    assert window == "14:00-15:00"


def test_window_inbox_parsing():
    inbox = (
        "From: Marek Sould <m.sould@kesterline.example>\n"
        "Sent: 2026-08-24 08:12\n"
        "Subject: Invitation — Quarterly budget review\n"
        "Response: ACCEPTED\n"
        "When: Tuesday 10:00-11:00\n\n"
        "From: I. Vane <i.vane@example>\n"
        "Response: TENTATIVE\n"
        "When: Tuesday 13:00-14:00\n\n"
        "From: Someone Else\n"
        "Response: ACCEPTED\n"
        "When: Wednesday 09:00-10:00\n"
    )
    window = tb.find_meeting_window(
        "Tuesday",
        "09:00",
        "15:00",
        duration_minutes=60,
        people=["ada"],
        inbox=inbox,
        schedules={"ada": {"busy": []}},
    )
    assert window == "09:00-10:00"  # clean, before the accepted 10:00 block


def test_window_requires_people():
    with pytest.raises(tb.ToolboxError):
        tb.find_meeting_window("Tuesday", "12:00", "14:00", people=None)


# ----------------------------------------------------------------------
# phase 3: meeting points
# ----------------------------------------------------------------------


def test_meeting_point_median():
    point = tb.meeting_point(
        "Wednesday",
        [0, 3],
        ["cira", "iris"],
        locations={"cira": {"x": 0, "y": 6}, "iris": {"x": 9, "y": 0}},
    )
    assert point == (0, 3)


def test_meeting_point_even_count():
    point = tb.meeting_point(
        "Wednesday",
        [2, 2],
        ["a", "b", "c"],
        locations={
            "a": {"x": 8, "y": 2},
            "b": {"x": 2, "y": 8},
            "c": {"x": 8, "y": 8},
        },
    )
    assert point == (2, 2)


def test_meeting_point_tool_returns_bracket_format(client):
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "find_meeting_point",
                "arguments": {
                    "day": "Wednesday",
                    "position": [0, 3],
                    "people": ["cira", "iris"],
                    "locations": {"cira": {"x": 0, "y": 6}, "iris": {"x": 9, "y": 0}},
                },
            },
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == "[0, 3]"


# ----------------------------------------------------------------------
# phase 3: outings
# ----------------------------------------------------------------------

OUTING_VENUES = {
    "day": "Monday",
    "venues": [
        {"name": "Far Away", "x": 9, "y": 9, "available": [["14:00", "18:00"]]},
        {"name": "Nearby", "x": 0, "y": 0, "available": [["14:00", "18:00"]]},
        {"name": "Too Late", "x": 0, "y": 0, "available": [["15:00", "16:00"]]},
    ],
}


def test_plan_outing_minimises_total_journey():
    plan = tb.plan_outing(
        "Monday",
        [0, 0],
        ["ada"],
        "13:00",
        "18:00",
        duration_minutes=60,
        venues=OUTING_VENUES,
        schedules={"ada": {"busy": []}},
        locations={"ada": {"x": 0, "y": 0}},
    )
    assert plan["window"] == "13:00-14:00"
    assert plan["venue"] == "Nearby"
    assert plan["point"] == "[0, 0]"
    assert plan["total_travel"] == 0


def test_plan_outing_excludes_venue_closed_at_eat_hour():
    with pytest.raises(tb.ToolboxError):
        tb.plan_outing(
            "Monday",
            [0, 0],
            ["ada"],
            "13:00",
            "18:00",
            duration_minutes=60,
            venues={"day": "Monday", "venues": [{"name": "Too Late", "x": 0, "y": 0, "available": [["15:00", "16:00"]]}]},
            schedules={"ada": {"busy": []}},
            locations={"ada": {"x": 0, "y": 0}},
        )


def test_plan_outing_pulls_point_toward_venue():
    # Only one venue, far from everyone: the meeting point must sit between
    # them (median), not at the friends' own median without the venue.
    plan = tb.plan_outing(
        "Monday",
        [0, 0],
        ["ada"],
        "13:00",
        "18:00",
        duration_minutes=60,
        venues={"day": "Monday", "venues": [{"name": "Far Away", "x": 9, "y": 9, "available": [["14:00", "18:00"]]}]},
        schedules={"ada": {"busy": []}},
        locations={"ada": {"x": 0, "y": 0}},
    )
    assert plan["venue"] == "Far Away"
    assert plan["point"] == "[0, 0]"
    assert plan["total_travel"] == 18
