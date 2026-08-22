import time as _time

from fastapi import APIRouter

from app.simple_kancheong import UNREACHABLE, solve_case

router = APIRouter()

# The challenge enforces a single hard 10-second cutoff for the whole batch.
# Keep a little headroom and make sure a slow case can never zero the request.
BATCH_BUDGET_SEC = 9.0


def _complexity(case: dict) -> int:
    if not isinstance(case, dict):
        return 0
    nodes = case.get("nodes")
    edges = case.get("edges")
    return (len(nodes) if isinstance(nodes, list) else 0) * (
        len(edges) if isinstance(edges, list) else 0
    )


@router.post("/kan-cheong-delivery-driver")
def kan_cheong_delivery_driver(request: dict):
    """Batch route: solve each case independently, easy cases first."""
    deadline = _time.monotonic() + BATCH_BUDGET_SEC

    # Solve small cases first so a single huge case cannot starve the rest.
    ordered = sorted(request.items(), key=lambda item: _complexity(item[1]))

    results: dict = {}
    for case_id, case in ordered:
        if _time.monotonic() > deadline:
            results[case_id] = UNREACHABLE
            continue
        results[case_id] = solve_case(case, deadline)

    return results
