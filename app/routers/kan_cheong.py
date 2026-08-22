from fastapi import APIRouter

from app.kan_cheong import solve_case

router = APIRouter()


@router.post("/kan-cheong-delivery-driver")
async def kan_cheong_delivery_driver(request: dict):
    """Batch route: solve each case independently and return the same id map."""
    return {case_id: solve_case(case) for case_id, case in request.items()}
