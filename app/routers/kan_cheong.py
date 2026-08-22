import asyncio
from concurrent.futures import ProcessPoolExecutor

from fastapi import APIRouter

from app.kan_cheong import solve_case  # wherever solve_case actually lives

router = APIRouter()

TOTAL_BUDGET_SEC = 9.0  # leave headroom under the 10s hard cutoff
_executor = ProcessPoolExecutor()  # module-level: reused across requests, not recreated per call


@router.post("/kan-cheong-delivery-driver")
async def kan_cheong_delivery_driver(request: dict):
    """Batch route: solve each case independently and return the same id map."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOTAL_BUDGET_SEC

    async def run_case(case_id: str, case: dict):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return case_id, {"total_duration_sec": None, "arrival_time": None, "path": []}
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(_executor, solve_case, case),
                timeout=remaining,
            )
        except TimeoutError:
            result = {"total_duration_sec": None, "arrival_time": None, "path": []}
        return case_id, result

    results = await asyncio.gather(
        *(run_case(cid, case) for cid, case in request.items())
    )
    return dict(results)