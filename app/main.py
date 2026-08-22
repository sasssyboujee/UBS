import base64
import json

from app import routers
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.mcp_server import handle_rpc, sse_endpoint_stream, sse_response_stream
from app.models import MoveRequest, MoveResponse
from app.strategy import choose_action

app = FastAPI(
    title="UBS Global Coding Challenge API",
    description="Production-grade FastAPI service",
    version="1.0.0",
)
for router in routers:
    app.include_router(router)

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

# Strict Pydantic v2 data models
class HealthResponse(BaseModel):
    status: str = Field(..., description="Current health status of the API")
    version: str = Field(..., description="API version")

class HelloRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="Name to greet")

    @field_validator('name')
    @classmethod
    def name_must_not_contain_numbers(cls, v: str) -> str:
        if any(char.isdigit() for char in v):
            raise ValueError('Name must not contain numbers')
        # Sanitize input implicitly by stripping whitespace
        return v.strip()

class HelloResponse(BaseModel):
    greeting: str = Field(..., description="Greeting message")

class SolveRequest(BaseModel):
    payload: str = Field(..., description="Base64 encoded payload")

class AdaptOutput(BaseModel):
    id: str
    name: str
    action: str
    priority: int

class SolveResponse(BaseModel):
    adaptOutput: AdaptOutput

class UserInput(BaseModel):
    id: str
    fullName: str

class MetadataInput(BaseModel):
    priority: str

class AdaptInputInner(BaseModel):
    user: UserInput
    action: str
    metadata: MetadataInput

class DecodedPayload(BaseModel):
    adaptInput: AdaptInputInner

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
            detail="Cannot greet 'error'"
        )
    return HelloResponse(greeting=f"Hello, {request.name}!")




@app.api_route("/mcp", methods=["GET", "POST"])
async def mcp_endpoint(request: Request):
    """Tool-box / Nursery challenge: minimal MCP server (Streamable HTTP)."""
    accept = request.headers.get("accept", "")

    if request.method == "GET":
        if "text/event-stream" in accept:
            return StreamingResponse(sse_endpoint_stream(), media_type="text/event-stream")
        return JSONResponse({"service": "nursery-mcp", "status": "ok"})

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    result = handle_rpc(body)
    if result is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    if "text/event-stream" in accept:
        return StreamingResponse(sse_response_stream(result), media_type="text/event-stream")
    return JSONResponse(result)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
