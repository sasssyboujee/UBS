from fastapi import APIRouter
from app.models import HelloRequest, MoveRequest, MoveResponse
from fastapi import FastAPI, HTTPException, Request, status


router = APIRouter()


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

@router.post("/solve", response_model=SolveResponse)
async def solve_challenge(request: SolveRequest):
    try:
        data = _decode_payload(request.payload)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid base64 payload or non-JSON content"
        )

    try:
        decoded_payload = DecodedPayload(**data)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid payload structure: {e!s}"
        )

    adapt_input = decoded_payload.adaptInput
    priority_map = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    priority_val = priority_map.get(adapt_input.metadata.priority.upper(), 0)

    output = AdaptOutput(
        id=adapt_input.user.id,
        name=adapt_input.user.fullName,
        action=adapt_input.action.lower(),
        priority=priority_val
    )

    return SolveResponse(adaptOutput=output)

