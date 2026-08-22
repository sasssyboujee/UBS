from fastapi import APIRouter

from app.ghost_chains import scorer
from app.models import (
    GhostHealthResponse,
    GhostResetRequest,
    GhostResetResponse,
    GhostTransactionResult,
    GhostTransactionsRequest,
    GhostTransactionsResponse,
)

router = APIRouter()


@router.get("/ghost-chains/health", response_model=GhostHealthResponse)
async def ghost_chains_health():
    return GhostHealthResponse(status="ok")


@router.post("/ghost-chains/reset", response_model=GhostResetResponse)
async def ghost_chains_reset(request: GhostResetRequest):
    if request.clearTransactions:
        scorer.reset()
    return GhostResetResponse(clearTransactions=request.clearTransactions)


@router.post("/ghost-chains/transactions", response_model=GhostTransactionsResponse)
async def ghost_chains_transactions(request: GhostTransactionsRequest):
    results = scorer.process(request.transactions)
    return GhostTransactionsResponse(
        transactions=[
            GhostTransactionResult(txId=tx_id, riskScore=round(float(score), 10))
            for tx_id, score in results
        ]
    )
