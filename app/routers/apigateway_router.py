import base64
import json
import math

from fastapi import APIRouter, HTTPException, status

from app.models import (
    AdaptOutput,
    DecodedPayload,
    Heartbeat,
    SloOutput,
    SloQuery,
    SolveRequest,
    SolveResponse,
)

router = APIRouter()

PRIORITY_MAP = {
    "HIGH": 3,
    "MEDIUM": 2,
    "LOW": 1,
}


def _decode_payload(raw: str) -> dict:
    text = raw.strip()

    # First allow plain JSON.
    try:
        data = json.loads(text)

        if isinstance(data, dict):
            return data

    except json.JSONDecodeError:
        pass

    # Otherwise decode Base64.
    padded = text + "=" * (-len(text) % 4)

    try:
        decoded_bytes = base64.b64decode(
            padded,
            validate=True,
        )

        data = json.loads(decoded_bytes.decode("utf-8"))

        if isinstance(data, dict):
            return data

    except (
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        pass

    raise ValueError("Invalid Base64 payload or JSON")


def _adapt_input(adapt_input) -> AdaptOutput:
    priority = adapt_input.metadata.priority.upper()

    if priority not in PRIORITY_MAP:
        raise ValueError(
            f"Invalid priority: {adapt_input.metadata.priority}"
        )

    return AdaptOutput(
        id=adapt_input.user.id,
        name=adapt_input.user.fullName,
        action=adapt_input.action.lower(),
        priority=PRIORITY_MAP[priority],
    )


def _compute_slo(
    heartbeats: list[Heartbeat],
    slo_query: SloQuery | None,
) -> SloOutput | None:

    if not heartbeats:
        return None if slo_query is None else SloOutput(
            availability=0.0,
            p95LatencyMs=0,
        )

    if slo_query is None:
        window = heartbeats
    else:
        window = [
            hb
            for hb in heartbeats
            if hb.timestamp >= slo_query.since
        ]

    if not window:
        return SloOutput(
            availability=0.0,
            p95LatencyMs=0,
        )

    # Availability
    ok_count = sum(
        1
        for hb in window
        if hb.status.strip().upper() == "OK"
    )

    availability = ok_count / len(window)
    # P95
    latencies = sorted(
        hb.latencyMs
        for hb in window
    )

    rank = math.ceil(0.95 * len(latencies))
    p95_latency = latencies[rank - 1]

    return SloOutput(
        availability=availability,
        p95LatencyMs=p95_latency,
    )


@router.post(
    "/solve",
    response_model=SolveResponse,
    response_model_exclude_none=True,
)
async def solve_challenge(
    request: SolveRequest,
):
    try:
        data = _decode_payload(request.payload)

        decoded = DecodedPayload(**data)

        adapt_output = _adapt_input(
            decoded.adaptInput
        )

        slo_output = _compute_slo(
            decoded.heartbeats,
            decoded.sloQuery,
        )

        return SolveResponse(
            adaptOutput=adapt_output,
            sloOutput=slo_output,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    except TypeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid payload structure: {e}",
        )