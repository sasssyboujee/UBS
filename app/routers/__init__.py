from . import apigateway, ghost_chains, showdown

routers = [apigateway.router, showdown.router, ghost_chains.router]
