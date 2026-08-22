import json
import logging

from fastapi import APIRouter

from app.models import MoveRequest, MoveResponse
from app.strategy import choose_action

router = APIRouter()
logger = logging.getLogger("showdown")


@router.post("/move", response_model=MoveResponse, response_model_exclude_none=True)
async def showdown_move(request: MoveRequest):
    """SHOWDOWN challenge: reply with one of the coordinator's legal_actions.

    Every request/response pair is logged so a lost leg can be replayed and
    the leg's table rule learned from the coordinator's own showdown data.
    """
    try:
        response = choose_action(request)
    except Exception:
        logger.exception(
            "move_error req=%s",
            json.dumps(request.model_dump(), separators=(",", ":")),
        )
        raise

    logger.info(
        "move req=%s resp=%s",
        json.dumps(request.model_dump(), separators=(",", ":")),
        json.dumps(response.model_dump(), separators=(",", ":")),
    )
    return response
