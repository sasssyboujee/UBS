"""Pydantic models for the challenge endpoints.

The coordinators send rich, evolving payloads; every schema here is tolerant
of unknown or newly added fields so a 422 never occurs because of extra data.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# SHOWDOWN challenge models
# ---------------------------------------------------------------------------


class PlayerState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    seat: int = 0
    name: str = ""
    folded: bool = False
    chip_delta: int = 0
    bet_this_round: int = 0
    stack: int = 0
    all_in: bool = False
    busted: bool = False


class ActionLog(BaseModel):
    model_config = ConfigDict(extra="ignore")

    round: str = ""
    seat: int = -1
    action: str = ""
    amount: int | None = None


class HandResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hand_number: int = 0
    community_number: int | None = None
    winners: list[int] = Field(default_factory=list)
    pot: int = 0
    shown_numbers: dict[str, int] = Field(default_factory=dict)
    actions: list[ActionLog] = Field(default_factory=list)


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    protocol_version: int = 2
    match_id: str = ""
    phase: int = 1
    table_rule: str = "standard"
    small_blind: int = 1
    big_blind: int = 2
    starting_stack: int = 200
    your_stack: int = 200
    hand_number: int = 0
    total_hands: int = 100
    round: str = "pre_reveal"
    your_number: int | None = None
    community_number: int | None = None
    your_seat: int = 0
    button_seat: int = 0
    pot: int = 0
    to_call: int = 0
    min_raise_to: int | None = None
    max_raise_to: int | None = None
    legal_actions: list[str] = Field(default_factory=list)
    players: list[PlayerState] = Field(default_factory=list)
    current_hand_actions: list[ActionLog] = Field(default_factory=list)
    recent_hands: list[HandResult] = Field(default_factory=list)


class MoveResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["check", "call", "bet", "raise", "fold"]
    amount: int | None = None


# ---------------------------------------------------------------------------
# Adaptive API Gateway challenge models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = Field(..., description="Current health status of the API")
    version: str = Field(..., description="API version")


class HelloRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="Name to greet")

    @field_validator("name")
    @classmethod
    def name_must_not_contain_numbers(cls, v: str) -> str:
        if any(char.isdigit() for char in v):
            raise ValueError("Name must not contain numbers")
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


class SloOutput(BaseModel):
    availability: float = 0.0
    p95LatencyMs: int = 0


class SolveResponse(BaseModel):
    adaptOutput: AdaptOutput
    sloOutput: SloOutput | None = None


class UserInput(BaseModel):
    id: str
    fullName: str


class MetadataInput(BaseModel):
    priority: str


class AdaptInputInner(BaseModel):
    user: UserInput
    action: str
    metadata: MetadataInput


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: str = ""
    timestamp: int = 0
    latencyMs: int = 0
    status: str = ""


class SloQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: str = ""
    since: int = 0


class DecodedPayload(BaseModel):
    adaptInput: AdaptInputInner
    heartbeats: list[Heartbeat] = Field(default_factory=list)
    sloQuery: SloQuery | None = None


# ---------------------------------------------------------------------------
# Ghost Chains challenge models
# ---------------------------------------------------------------------------


class GhostTransaction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    txId: str
    fromUserId: str
    toUserId: str
    amount: float
    createdAt: str
    ipAddress: str | None = None
    deviceId: str | None = None


class GhostTransactionsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transactions: list[GhostTransaction] = Field(default_factory=list)


class GhostTransactionResult(BaseModel):
    txId: str
    riskScore: float


class GhostTransactionsResponse(BaseModel):
    transactions: list[GhostTransactionResult]


class GhostResetRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clearTransactions: bool = True


class GhostResetResponse(BaseModel):
    clearTransactions: bool


class GhostHealthResponse(BaseModel):
    status: str
