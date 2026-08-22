from . import (
    apigateway_router,
    ghost_chains,
    kan_cheong,
    showdown_router,
    stonks_router,
)

routers = [
    apigateway_router.router,
    showdown_router.router,
    ghost_chains.router,
    kan_cheong.router,
    stonks_router.router,
]
