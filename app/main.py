import base64
import json

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.models import MoveRequest, MoveResponse
from app.strategy import choose_action

app = FastAPI(
    title="UBS Global Coding Challenge API",
    description="Production-grade FastAPI service",
    version="1.0.0",
)

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

@app.post("/solve", response_model=SolveResponse)
async def solve_challenge(request: SolveRequest):
    try:
        decoded_bytes = base64.b64decode(request.payload)
        decoded_str = decoded_bytes.decode('utf-8')
        data = json.loads(decoded_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid base64 payload or non-JSON content"
        )

    try:
        decoded_payload = DecodedPayload(**data)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid payload structure: {e!s}"
        )

    adapt_input = decoded_payload.adaptInput
    priority_map = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    priority_val = priority_map.get(adapt_input.metadata.priority.upper(), 0)

    output = AdaptOutput(
        id=adapt_input.user.id,
        name=adapt_input.user.fullName,
        action=adapt_input.action.lower(),
        priority=priority_val
    )

    return SolveResponse(adaptOutput=output)

@app.post("/move", response_model=MoveResponse, response_model_exclude_none=True)
async def showdown_move(request: MoveRequest):
    """SHOWDOWN challenge: reply with one of the coordinator's legal_actions."""
    return choose_action(request)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
