"""Decision logic for the SHOWDOWN betting-game bot.

Pure functions only: no I/O, no mutable state. The coordinator calls
POST /move once per turn, so decisions must be fast and side-effect-free.

The game is heads-up no-limit with one secret number (1-13) per player and
one community number revealed halfway through the hand. At showdown a pair
(your number == community number) beats any non-pair; otherwise the higher
number wins; ties split the pot.

Strategy outline
----------------
* Correct showdown equities vs a random opponent.
* When facing aggression, estimate the opponent's *range* instead of assuming
  a random hand: a raise/bet is skewed toward strong cards, pairs, and a few
  bluffs. This keeps us from paying off big bets with medium holdings.
* Value-bet strong hands, bluff occasionally, and never fold a pair.
* Never re-raise a non-pair into a raise post-reveal (opponent likely paired).
"""

from __future__ import annotations

import zlib

from app.models import MoveRequest, MoveResponse

KNOWN_TABLE_RULES = {"standard"}


def pre_reveal_equity(card: int) -> float:
    """Win probability (ties split) for a card vs a random opponent pre-reveal.

    Derived by averaging ``post_reveal_equity`` over the 13 community numbers:
    (22 * card + 15) / 338.
    """
    return (22 * card + 15) / 338.0


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


def _last_opponent_action(req: MoveRequest) -> str | None:
    """Most recent opponent action in the current hand, if any."""
    for action in reversed(req.current_hand_actions or []):
        if action.seat != req.your_seat:
            return action.action
    return None


def _showdown_value(card: int, opp: int, community: int) -> float:
    """Our share (1 win, 0 loss, 0.5 tie) at showdown vs one opponent card."""
    we_pair = card == community
    opp_pair = opp == community
    if we_pair and opp_pair:
        return 0.5
    if we_pair:
        return 1.0
    if opp_pair:
        return 0.0
    if card > opp:
        return 1.0
    if card < opp:
        return 0.0
    return 0.5


def _range_equity(card: int, community: int | None, opp_weights: dict[int, float]) -> float:
    """Equity vs a weighted opponent-card distribution.

    ``community`` is None pre-reveal, in which case we average over all 13
    possible community numbers.
    """
    if community is None:
        total = 0.0
        weight_sum = 0.0
        for opp, weight in opp_weights.items():
            if weight <= 0:
                continue
            equity = 0.0
            for comm in range(1, 14):
                equity += _showdown_value(card, opp, comm)
            total += weight * (equity / 13.0)
            weight_sum += weight
        return total / weight_sum if weight_sum else 0.0

    total = 0.0
    weight_sum = 0.0
    for opp, weight in opp_weights.items():
        if weight <= 0:
            continue
        total += weight * _showdown_value(card, opp, community)
        weight_sum += weight
    return total / weight_sum if weight_sum else 0.0


def _pre_reveal_opp_weights() -> dict[int, float]:
    """Assumed range for a pre-reveal raise: strong cards plus a few bluffs."""
    weights = {card: 0.0 for card in range(1, 14)}
    for card in range(8, 14):
        weights[card] = 1.0
    for card in (1, 2, 3):
        weights[card] = 0.15
    return weights


def _post_reveal_opp_weights(community: int, raised: bool) -> dict[int, float]:
    """Assumed range for a post-reveal bet (raised=False) or raise (raised=True).

    Pairs always carry full weight; value non-pairs depend on how much
    strength the opponent has shown; a small bluff component is included.
    """
    weights = {card: 0.0 for card in range(1, 14)}
    for card in range(1, 14):
        if card == community:
            weights[card] = 1.0
            continue
        equity = post_reveal_equity(card, community)
        if raised:
            if equity > 0.75:
                weights[card] = 1.0
            elif equity < 0.3:
                weights[card] = 0.12
        else:
            if equity > 0.62:
                weights[card] = 1.0
            elif equity < 0.3:
                weights[card] = 0.25
    return weights


def _clamp_raise(req: MoveRequest, pot_fraction: float) -> int | None:
    """Total amount for this betting round, sized as a fraction of the pot."""
    our_bet = _our_bet_this_round(req)
    target = our_bet + round((req.pot or 0) * pot_fraction)
    if req.min_raise_to is None or req.max_raise_to is None:
        return target
    return max(req.min_raise_to, min(target, req.max_raise_to))


def _chance(req: MoveRequest, key: str, percent: int) -> bool:
    """Deterministic pseudo-random coin flip, stable per match/hand/round/key."""
    seed = f"{req.match_id}:{req.hand_number}:{req.round}:{key}"
    return zlib.crc32(seed.encode("utf-8")) % 100 < percent


def _pre_reveal(req: MoveRequest, legal: set[str]) -> MoveResponse:
    card = req.your_number or 1
    to_call = req.to_call or 0
    pot = req.pot or 0
    stack = req.your_stack or 0
    is_sb = req.your_seat == req.button_seat

    if to_call == 0:
        # We are the big blind (or otherwise last to act pre-reveal). The
        # small blind's call caps their range, so raise a wide value range.
        if "raise" in legal:
            if card >= 8:
                return MoveResponse(action="raise", amount=_clamp_raise(req, 0.6))
            if card <= 3 and _chance(req, "bluff_pre", 15):
                return MoveResponse(action="raise", amount=_clamp_raise(req, 0.5))
        return MoveResponse(action="check")

    if is_sb and to_call == 1 and pot == 3:
        # Small blind's first decision: no aggression has happened yet.
        if card >= 9 and "raise" in legal:
            return MoveResponse(action="raise", amount=_clamp_raise(req, 0.6))
        if card >= 4:
            return MoveResponse(action="call")
        return MoveResponse(action="fold")

    # Facing a raise: judge our hand against a strong range, not a random one.
    equity = _range_equity(card, None, _pre_reveal_opp_weights())
    odds = pot_odds(to_call, pot)
    if equity > odds + 0.04:
        # Re-raise only the nuts once; keep pots small with everything else.
        if card == 13 and "raise" in legal and to_call < 0.3 * stack and pot < 0.25 * stack:
            return MoveResponse(action="raise", amount=_clamp_raise(req, 0.7))
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

    if card == community:
        # Huge favourite: value-bet and never fold.
        if to_call == 0:
            if "bet" in legal:
                return MoveResponse(action="bet", amount=_clamp_raise(req, 0.6))
            return MoveResponse(action="check")
        if "raise" in legal and to_call < 0.5 * stack:
            return MoveResponse(action="raise", amount=_clamp_raise(req, 0.8))
        return MoveResponse(action="call")

    equity = post_reveal_equity(card, community)

    if to_call == 0:
        # First to act post-reveal.
        if "bet" in legal and equity > 0.6:
            return MoveResponse(action="bet", amount=_clamp_raise(req, 0.5))
        if "bet" in legal and equity < 0.3 and _chance(req, "bluff_post", 12):
            return MoveResponse(action="bet", amount=_clamp_raise(req, 0.5))
        return MoveResponse(action="check")

    # Facing a bet/raise: evaluate against the opponent's likely range.
    last_opp = _last_opponent_action(req)
    opp_weights = _post_reveal_opp_weights(community, last_opp == "raise")
    range_equity = _range_equity(card, community, opp_weights)
    odds = pot_odds(to_call, pot)

    if range_equity > odds + 0.03:
        # Raise once over a first bet with a very strong non-pair; never
        # re-raise a raise with a non-pair (opponent likely has a pair).
        if (last_opp == "bet" and card >= 12 and community < card
                and "raise" in legal and to_call < 0.25 * stack):
            return MoveResponse(action="raise", amount=_clamp_raise(req, 0.6))
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
        if resp.action in ("bet", "raise"):
            if (resp.amount is not None
                    and req.min_raise_to is not None
                    and req.max_raise_to is not None
                    and req.min_raise_to <= resp.amount <= req.max_raise_to):
                return resp
        else:
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
