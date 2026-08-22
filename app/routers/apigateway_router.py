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

PRIORITY_MAP = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _decode_payload(raw: str) -> dict:
    """Decode the challenge payload: base64 (standard or URL-safe) or raw JSON."""
    text = raw.strip()
    candidates = [text]
    if len(text) % 4:
        candidates.append(text + "=" * (4 - len(text) % 4))

    for candidate in candidates:
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = json.loads(decoder(candidate).decode("utf-8"))
                if isinstance(decoded, dict):
                    return decoded
            except ValueError:
                continue

    # Fallback: the payload may already be plain JSON.
    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            return decoded
    except ValueError:
        pass

    raise ValueError("payload is not valid base64-encoded JSON")


def _compute_slo(
    heartbeats: list[Heartbeat], slo_query: SloQuery | None
) -> SloOutput | None:
    """Compute availability and p95 latency over the SLO query window.

    The window is every heartbeat for the queried service whose timestamp is
    greater than or equal to ``slo_query.since`` (boundary inclusive). When no
    SLO query is supplied but heartbeats exist, the whole heartbeat stream is
    used; when neither exists we report nothing so Phase 1 payloads keep the
    exact legacy response shape.
    """
    if slo_query is None and not heartbeats:
        return None

    if slo_query is None:
        window = list(heartbeats)
    else:
        window = [
            hb
            for hb in heartbeats
            if hb.service == slo_query.service and hb.timestamp >= slo_query.since
        ]

    total = len(window)
    if total == 0:
        return SloOutput(availability=0.0, p95LatencyMs=0)

    ok_count = sum(1 for hb in window if hb.status.upper() == "OK")
    availability = ok_count / total

    # Nearest-rank p95 over the whole window (failures still consume latency).
    latencies = sorted(hb.latencyMs for hb in window)
    p95_index = math.ceil(0.95 * total) - 1
    return SloOutput(availability=availability, p95LatencyMs=latencies[p95_index])


@router.post("/solve", response_model=SolveResponse, response_model_exclude_none=True)
async def solve_challenge(request: SolveRequest):
    try:
        data = _decode_payload(request.payload)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid base64 payload or non-JSON content",
        )

    try:
        decoded_payload = DecodedPayload(**data)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid payload structure: {e!s}",
        )

    adapt_input = decoded_payload.adaptInput
    priority_val = PRIORITY_MAP.get(adapt_input.metadata.priority.upper(), 0)

    output = AdaptOutput(
        id=adapt_input.user.id,
        name=adapt_input.user.fullName,
        action=adapt_input.action.lower(),
        priority=priority_val,
    )

    slo_output = _compute_slo(decoded_payload.heartbeats, decoded_payload.sloQuery)

    return SolveResponse(adaptOutput=output, sloOutput=slo_output)
