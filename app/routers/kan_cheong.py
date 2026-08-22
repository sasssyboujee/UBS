from fastapi import APIRouter

from app.simple_kancheong import solve_case

router = APIRouter()


@router.post("/kan-cheong-delivery-driver")
async def kan_cheong_delivery_driver(request: dict):
    """Batch route: solve each case independently and return the same id map."""
<<<<<<< HEAD
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
=======
    return {case_id: solve_case(case) for case_id, case in request.items()}
>>>>>>> 88a3e8d3a6e260cb60b952b648f8a717b7311b35
