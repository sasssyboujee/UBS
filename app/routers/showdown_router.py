from fastapi import APIRouter

from app.models import MoveRequest, MoveResponse
from app.strategy import choose_action

router = APIRouter()


@router.post("/move", response_model=MoveResponse, response_model_exclude_none=True)
async def showdown_move(request: MoveRequest):
    """SHOWDOWN challenge: reply with one of the coordinator's legal_actions."""
    return choose_action(request)
