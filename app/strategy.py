"""Decision logic for the SHOWDOWN betting-game bot.

Pure functions only: no I/O, no mutable state. The coordinator calls
POST /move once per turn, so decisions must be fast and side-effect-free.

The game is heads-up no-limit with one secret number (1-13) per player and
one community number revealed halfway through the hand. At showdown a pair
(your number == community number) beats any non-pair; otherwise the higher
number wins; ties split the pot.
"""

from __future__ import annotations

import zlib

from app.models import MoveRequest, MoveResponse

KNOWN_TABLE_RULES = {"standard"}


def pre_reveal_equity(card: int) -> float:
    """Win probability (ties split) for a card vs a random opponent pre-reveal."""
    return (11 * card + 1) / 169.0


def post_reveal_equity(card: int, community: int) -> float:
    """Win probability (ties split) for a card vs a random opponent post-reveal."""
    if card == community:
        # A pair only loses if the opponent also pairs, and that is a split.
        return 25.0 / 26.0
    wins = (card - 1) - (1 if community < card else 0)
    return (wins + 0.5) / 13.0


def pot_odds(to_call: int, pot: int) -> float:
    """Fraction of the final pot we must contribute to stay in the hand."""
    if to_call <= 0:
        return 0.0
    return to_call / (pot + to_call)


def _our_player(req: MoveRequest):
    for player in req.players:
        if player.name == "you":
            return player
    for player in req.players:
        if player.seat == req.your_seat:
            return player
    return None


def _our_bet_this_round(req: MoveRequest) -> int:
    player = _our_player(req)
    return player.bet_this_round if player else 0


def _active_opponents(req: MoveRequest) -> int:
    return sum(
        1
        for p in req.players
        if p.name != "you" and not p.folded and not p.busted
    )


def opponent_aggression(req: MoveRequest) -> float:
    """Opponent raises per hand over recent history plus the current hand (0..1)."""
    raises = 0
    hands = 0
    for hand in req.recent_hands or []:
        hands += 1
        for action in hand.actions or []:
            if action.seat != req.your_seat and action.action == "raise":
                raises += 1
    for action in req.current_hand_actions or []:
        if action.seat != req.your_seat and action.action == "raise":
            raises += 1
    if hands == 0:
        return 0.3
    return min(1.0, raises / hands)


def _chance(req: MoveRequest, key: str, percent: int) -> bool:
    """Deterministic pseudo-random coin flip, stable per match/hand/round/key."""
    seed = f"{req.match_id}:{req.hand_number}:{req.round}:{key}"
    return zlib.crc32(seed.encode("utf-8")) % 100 < percent


def _clamp(value: float, low: int | None, high: int | None) -> int | None:
    if low is None or high is None:
        return int(value)
    return max(low, min(int(value), high))


def _sized_raise(req: MoveRequest, pot_fraction: float) -> int | None:
    """Total amount for this betting round, sized as a fraction of the pot."""
    our_bet = _our_bet_this_round(req)
    target = our_bet + round(req.pot * pot_fraction)
    return _clamp(target, req.min_raise_to, req.max_raise_to)


def _pre_reveal(req: MoveRequest, legal: set[str]) -> MoveResponse:
    card = req.your_number or 1
    to_call = req.to_call or 0
    pot = req.pot or 0
    stack = req.your_stack or 0
    equity = pre_reveal_equity(card)

    if to_call == 0:
        # Free to check: we are last to act pre-reveal (big blind).
        if "raise" in legal and card >= 11:
            return MoveResponse(action="raise", amount=_sized_raise(req, 1.0))
        if "raise" in legal and card >= 8:
            return MoveResponse(action="raise", amount=_sized_raise(req, 0.66))
        if "raise" in legal and card <= 3 and _chance(req, "bluff_pre", 12):
            return MoveResponse(action="raise", amount=_sized_raise(req, 0.75))
        return MoveResponse(action="check")

    odds = pot_odds(to_call, pot)
    margin = 0.05 - 0.05 * opponent_aggression(req)

    if equity > odds + margin:
        # Ahead of pot odds: value-raise strong cards, otherwise call.
        if card >= 10 and "raise" in legal and to_call < 0.5 * stack:
            return MoveResponse(action="raise", amount=_sized_raise(req, 1.0))
        if card >= 8 and "raise" in legal and to_call <= 0.2 * stack:
            return MoveResponse(action="raise", amount=_sized_raise(req, 0.75))
        return MoveResponse(action="call")

    if equity > odds - 0.04 and to_call <= max(4, int(0.15 * stack)):
        # Marginal: pay small amounts to see the community number.
        return MoveResponse(action="call")

    return MoveResponse(action="fold")


def _post_reveal(req: MoveRequest, legal: set[str]) -> MoveResponse:
    card = req.your_number or 1
    community = req.community_number
    to_call = req.to_call or 0
    pot = req.pot or 0
    stack = req.your_stack or 0

    if community is None:
        # Malformed payload guard: treat as pre-reveal.
        return _pre_reveal(req, legal)

    paired = card == community

    if paired:
        # Huge favourite: value-bet and never fold.
        if to_call == 0 and "bet" in legal:
            return MoveResponse(action="bet", amount=_sized_raise(req, 0.6))
        if to_call == 0:
            return MoveResponse(action="check")
        if "raise" in legal and to_call < 0.6 * stack:
            return MoveResponse(action="raise", amount=_sized_raise(req, 0.9))
        return MoveResponse(action="call")

    equity = post_reveal_equity(card, community)
    margin = 0.06 - 0.05 * opponent_aggression(req)
    if community > card:
        margin += 0.06
    margin += 0.08 * max(0, _active_opponents(req) - 1)

    if to_call == 0:
        # First to act post-reveal.
        if "bet" in legal and card >= 10 and community < card:
            return MoveResponse(action="bet", amount=_sized_raise(req, 0.55))
        if "bet" in legal and card <= 4 and _chance(req, "bluff_post", 12):
            return MoveResponse(action="bet", amount=_sized_raise(req, 0.6))
        return MoveResponse(action="check")

    odds = pot_odds(to_call, pot)

    if equity > odds + margin:
        if card >= 11 and community < card and "raise" in legal and to_call < 0.5 * stack:
            return MoveResponse(action="raise", amount=_sized_raise(req, 0.75))
        return MoveResponse(action="call")

    if equity > odds - 0.03 and to_call <= max(3, int(0.08 * stack)):
        return MoveResponse(action="call")

    return MoveResponse(action="fold")


def _conservative(req: MoveRequest, legal: set[str]) -> MoveResponse:
    """Fallback for unknown table rules: never raise, only pay tiny amounts."""
    to_call = req.to_call or 0
    pot = req.pot or 0
    stack = req.your_stack or 0

    if to_call == 0:
        return MoveResponse(action="check")

    if to_call <= max(2, int(0.05 * pot)) and to_call <= 0.1 * stack:
        return MoveResponse(action="call")
    return MoveResponse(action="fold")


def _legalize(resp: MoveResponse, req: MoveRequest, legal: set[str]) -> MoveResponse:
    """Guarantee the response is one of the coordinator's legal_actions."""
    if resp.action in legal:
        return resp

    if not legal:
        return MoveResponse(action="call" if (req.to_call or 0) > 0 else "check")

    for fallback in ("check", "call", "fold"):
        if fallback in legal:
            return MoveResponse(action=fallback)
    return MoveResponse(action=min(legal))


def choose_action(req: MoveRequest) -> MoveResponse:
    """Pick the bot's next action for a /move request."""
    legal = set(req.legal_actions or [])

    if req.table_rule not in KNOWN_TABLE_RULES:
        resp = _conservative(req, legal)
    elif req.round == "pre_reveal":
        resp = _pre_reveal(req, legal)
    else:
        resp = _post_reveal(req, legal)

    return _legalize(resp, req, legal)
