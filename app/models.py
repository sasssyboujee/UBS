"""Pydantic models for the SHOWDOWN challenge move endpoint.

The coordinator sends a rich, evolving payload; every field is optional in
this schema so unknown or newly added fields never cause a 422.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
