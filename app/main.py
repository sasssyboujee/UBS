import logging
import json
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from app.mcp_server2 import mcp

from app.mcp_server import handle_rpc, sse_endpoint_stream, sse_response_stream
from app.models import (
    HealthResponse,
    HelloRequest,
    HelloResponse,
)
from app.routers import routers

logger = logging.getLogger("uvicorn.error")

app = FastAPI(
    title="UBS Global Coding Challenge API",
    description="Production-grade FastAPI service",
    version="1.0.0",
)
for router in routers:
    app.include_router(router)
app.mount("/mcp", mcp.sse_app())

# Strict Security: Enforce CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Exception handler for zero unhandled 500s
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "Internal Server Error", "message": "An unexpected error occurred."},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    logger.info(
        "422 VALIDATION path=%s body=%s errors=%s",
        request.url.path,
        body.decode("utf-8", errors="replace")[:2000],
        exc.errors(),
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": jsonable_encoder(exc.errors())},
    )


@app.get("/health", response_model=HealthResponse)
@app.get("/healthz", response_model=HealthResponse)
async def health_check():
    return HealthResponse(status="healthy", version="1.0.0")


@app.post("/hello", response_model=HelloResponse)
async def say_hello(request: HelloRequest):
    if request.name.lower() == "error":
        # Custom HTTP error mapping
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot greet 'error'",
        )
    return HelloResponse(greeting=f"Hello, {request.name}!")


@app.api_route("/mcp", methods=["GET", "POST"])
async def mcp_endpoint(request: Request):
    accept = request.headers.get("accept", "")

    if request.method == "GET":
        if "text/event-stream" in accept:
            return StreamingResponse(sse_endpoint_stream(), media_type="text/event-stream")
        return JSONResponse({"service": "school-days-mcp", "status": "ok"})

    # POST
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    result = handle_rpc(body)

    if result is None:
        return JSONResponse(content=None, status_code=202)

    if "text/event-stream" in accept:
        return StreamingResponse(sse_response_stream(result), media_type="text/event-stream")

    return JSONResponse(content=result)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
