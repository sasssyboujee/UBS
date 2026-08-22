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

router = APIRouter(prefix="/ghost-chains", tags=["Ghost Chains"])


@router.get("/health", response_model=GhostHealthResponse)
def ghost_chains_health():
    return GhostHealthResponse(status="ok")


@router.post("/reset", response_model=GhostResetResponse)
def ghost_chains_reset(request: GhostResetRequest):
    if request.clearTransactions:
        scorer.reset()
    return GhostResetResponse(clearTransactions=request.clearTransactions)


@router.post("/transactions", response_model=GhostTransactionsResponse)
def ghost_chains_transactions(request: GhostTransactionsRequest):
    results = scorer.process(request.transactions)
    return GhostTransactionsResponse(
        transactions=[
            GhostTransactionResult(txId=tx_id, riskScore=round(float(score), 10))
            for tx_id, score in results
        ]
    )
