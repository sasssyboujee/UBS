import app.routers.apigateway_router as apigateway_router
import app.routers.showdown_router as showdown_router
from . import ghost_chains, kan_cheong

routers = [
    apigateway_router.router,
    showdown_router.router,
    ghost_chains.router,
    kan_cheong.router,
]
