from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter

from app.kan_cheong import solve_case

router = APIRouter()

_executor = ThreadPoolExecutor(max_workers=8)

UNREACHABLE = {"total_duration_sec": None, "arrival_time": None, "path": []}


@router.post("/kan-cheong-delivery-driver")
async def kan_cheong_delivery_driver(request: dict):
    """Batch route: solve each case concurrently and return the same id map."""
    futures = {_executor.submit(solve_case, case): case_id for case_id, case in request.items()}
    results: dict[str, dict] = {}
    for future in as_completed(futures, timeout=9.0):
        case_id = futures[future]
        try:
            results[case_id] = future.result(timeout=0.1)
        except Exception:  # noqa: BLE001
            results[case_id] = UNREACHABLE
    # Fill in any cases that didn't finish
    for case_id in request:
        if case_id not in results:
            results[case_id] = UNREACHABLE
    return results

