"""Decision logic for the SHOWDOWN betting-game bot.

Phase 1 (``table_rule == "standard"``) uses exact showdown equities vs a
random opponent and range-aware decisions when facing aggression.

Phase 2 adds opaque ``table_rule`` codenames and multi-leg attempts. The rules
are never disclosed, so the bot learns each codename's showdown ordering from
the ``recent_hands`` the coordinator sends on every request, then plays an
adaptive strategy: cautious and exploratory while data is scarce, aggressive
only once the rule is understood.

Learning state lives in module memory only (no I/O). Decisions stay fast and
deterministic for a given state; the coordinator never retries, so recording
observations is safe.
"""

from __future__ import annotations

import json
import os
import tempfile
import zlib
from collections import OrderedDict
from pathlib import Path

from app.models import MoveRequest, MoveResponse

# Persisted learning state: the phase-2 docs guarantee that the leg order and
# each leg's table rule are identical on every retry, so models learned in one
# attempt stay valid for the next. Render's container filesystem survives
# between requests and attempts (it is only wiped on redeploy/restart), which
# lets retries compound instead of re-learning from scratch every time.
_STATE_PATH = Path(
    os.environ.get(
        "SHOWDOWN_STATE_PATH",
        str(Path(tempfile.gettempdir()) / "showdown_learn_state.json"),
    )
)

KNOWN_TABLE_RULES = {"standard"}

# ---------------------------------------------------------------------------
# Learned per-codename showdown model
# ---------------------------------------------------------------------------
# Hand types: 0..12 -> non-pair card 1..13, 13..25 -> paired card 1..13.
_HAND_TYPES = 26
_PRIOR_WEIGHT = 1.0
_POWER_EPS = 0.02

# codename -> {"wins": [float; 26], "games": [float; 26]}
_RULE_STATS: dict[str, dict[str, list[float]]] = {}

# codename -> {(our_card, opp_card, community): share} for exact recall of
# observed matchups. Some rules depend on the community card beyond pair
# status; remembering the precise outcome of seen triples beats any
# generalization for those.
_RULE_PAIRS: dict[str, dict[tuple[int, int, int], float]] = {}

# Opponent betting-range observations (the opponent plays the same way in
# every leg, so these are learned globally, not per codename).
_OPP_PRE_RAISE_CARDS: list[int] = []
_OPP_POST_BET_CARDS: list[int] = []
_OPP_POST_RAISE_CARDS: list[int] = []

# (match_id, leg_number) -> set of content hashes already folded into the model.
# recent_hands overlaps between requests, so without this every showdown would
# be counted many times. Hashing content (rather than tracking hand_number)
# keeps learning working even if the coordinator omits hand numbers.
_SEEN_HANDS: OrderedDict[tuple[str, int], set[int]] = OrderedDict()
_SEEN_MAX_KEYS = 32


def _reset_learning() -> None:
    """Clear all learned state (used by tests)."""
    _RULE_STATS.clear()
    _RULE_PAIRS.clear()
    _OPP_PRE_RAISE_CARDS.clear()
    _OPP_POST_BET_CARDS.clear()
    _OPP_POST_RAISE_CARDS.clear()
    _SEEN_HANDS.clear()
    try:
        _STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _save_state() -> None:
    """Persist learned models to disk (best effort, never raises)."""
    payload = {
        "rule_stats": {
            name: {"wins": stats["wins"], "games": stats["games"]}
            for name, stats in _RULE_STATS.items()
        },
        "rule_pairs": {
            name: {f"{c},{o},{m}": value for (c, o, m), value in pairs.items()}
            for name, pairs in _RULE_PAIRS.items()
        },
        "opp_pre_raise": list(_OPP_PRE_RAISE_CARDS),
        "opp_post_bet": list(_OPP_POST_BET_CARDS),
        "opp_post_raise": list(_OPP_POST_RAISE_CARDS),
    }
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _STATE_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")))
        tmp_path.replace(_STATE_PATH)
    except (OSError, ValueError):
        pass  # persistence must never break a /move reply


def _load_state() -> None:
    """Restore learned models from disk, merging into module memory."""
    try:
        payload = json.loads(_STATE_PATH.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return

    rule_stats = payload.get("rule_stats")
    if isinstance(rule_stats, dict):
        for name, entry in rule_stats.items():
            wins = entry.get("wins") if isinstance(entry, dict) else None
            games = entry.get("games") if isinstance(entry, dict) else None
            if (
                isinstance(wins, list)
                and isinstance(games, list)
                and len(wins) == _HAND_TYPES
                and len(games) == _HAND_TYPES
            ):
                _RULE_STATS[name] = {
                    "wins": [float(x) for x in wins],
                    "games": [float(x) for x in games],
                }

    rule_pairs = payload.get("rule_pairs")
    if isinstance(rule_pairs, dict):
        for name, entries in rule_pairs.items():
            if not isinstance(entries, dict):
                continue
            pairs: dict[tuple[int, int, int], float] = {}
            for key, value in entries.items():
                try:
                    c, o, m = (int(part) for part in key.split(","))
                except (AttributeError, ValueError):
                    continue
                pairs[(c, o, m)] = float(value)
            if pairs:
                _RULE_PAIRS.setdefault(name, {}).update(pairs)

    for field, target in (
        ("opp_pre_raise", _OPP_PRE_RAISE_CARDS),
        ("opp_post_bet", _OPP_POST_BET_CARDS),
        ("opp_post_raise", _OPP_POST_RAISE_CARDS),
    ):
        observed = payload.get(field)
        if isinstance(observed, list):
            target.extend(int(x) for x in observed if isinstance(x, int))


def _hand_index(card: int, paired: bool) -> int:
    return (card - 1) + (13 if paired else 0)


def _prior_power(idx: int) -> float:
    """Standard-rule strength of a hand type, used as the learning prior."""
    card = idx % 13 + 1
    if idx >= 13:
        return 25.0 / 26.0
    return (22 * card + 15) / 338.0


def _stats(codename: str) -> dict[str, list[float]]:
    stats = _RULE_STATS.get(codename)
    if stats is None:
        stats = {"wins": [0.0] * _HAND_TYPES, "games": [0.0] * _HAND_TYPES}
        _RULE_STATS[codename] = stats
    return stats


def _codename_power(codename: str, idx: int) -> float | None:
    """Laplace-smoothed strength of a hand type under a codename.

    Returns ``None`` when the codename has never been observed (no stats),
    otherwise blends observed wins/losses with the standard-rule prior so
    unseen hand types still have a sensible default.
    """
    stats = _RULE_STATS.get(codename)
    if stats is None:
        return None
    wins = stats["wins"][idx]
    games = stats["games"][idx]
    return (wins + _PRIOR_WEIGHT * _prior_power(idx)) / (games + _PRIOR_WEIGHT)


def _codename_observations(codename: str) -> int:
    """Number of showdown comparisons recorded for a codename."""
    stats = _RULE_STATS.get(codename)
    if stats is None:
        return 0
    return int(sum(stats["games"]) / 2.0)


# ---------------------------------------------------------------------------
# Observation recording
# ---------------------------------------------------------------------------


def _bump(stats: dict[str, list[float]], winner_idx: int, loser_idx: int, amount: float) -> None:
    stats["wins"][winner_idx] += amount
    stats["games"][winner_idx] += 1.0
    stats["games"][loser_idx] += 1.0


def _add_pair(pairs: dict[tuple[int, int, int], float], triple: tuple[int, int, int], value: float) -> None:
    """Store an observed (our, opp, community) showdown outcome."""
    if triple in pairs:
        pairs[triple] = (pairs[triple] + value) / 2.0
    else:
        pairs[triple] = value


def _remember(cards: list[int], card: int, cap: int = 300) -> None:
    """Append an observed card, keeping the list bounded."""
    cards.append(card)
    if len(cards) > cap:
        del cards[: len(cards) - cap]


def _to_int(value) -> int | None:
    """Coerce a model value (int, numeric string) to int; None on garbage."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _record_hand(req: MoveRequest, codename: str, hand) -> None:
    """Fold one showdown result into the codename model.

    The coordinator's exact field shapes vary between phases (winners as seats
    or names, numbers as ints or strings), so this is deliberately tolerant.
    """
    comm = _to_int(hand.community_number)
    if comm is None:
        return
    shown = hand.shown_numbers or {}
    if len(shown) < 2:
        return

    # Our number: prefer the "you" key, fall back to our seat as a string.
    our_num = _to_int(shown.get("you"))
    if our_num is None:
        our_num = _to_int(shown.get(str(req.your_seat)))
    if our_num is None:
        return

    # Opponent's number: the other key in shown_numbers.
    opp_key = next(
        (k for k in shown if k not in ("you", str(req.your_seat))), None
    )
    if opp_key is None:
        return
    opp_num = _to_int(shown.get(opp_key))
    if opp_num is None:
        return

    our_idx = _hand_index(our_num, our_num == comm)
    opp_idx = _hand_index(opp_num, opp_num == comm)

    winners = set(hand.winners or [])
    if not winners:
        return

    # Winners may be seats (ints) or names (strings); accept both.
    our_markers = {req.your_seat, "you", str(req.your_seat)}
    opp_markers = {1 - req.your_seat, opp_key}
    we_won = bool(winners & our_markers)
    opp_won = bool(winners & opp_markers)
    if not we_won and not opp_won:
        return

    stats = _stats(codename)
    pairs = _RULE_PAIRS.setdefault(codename, {})
    if we_won and opp_won:
        # Split pot: both hands have equal showdown value.
        _bump(stats, our_idx, opp_idx, 0.5)
        _bump(stats, opp_idx, our_idx, 0.5)
        _add_pair(pairs, (our_num, opp_num, comm), 0.5)
        _add_pair(pairs, (opp_num, our_num, comm), 0.5)
    elif we_won:
        _bump(stats, our_idx, opp_idx, 1.0)
        _add_pair(pairs, (our_num, opp_num, comm), 1.0)
    else:
        _bump(stats, opp_idx, our_idx, 1.0)
        _add_pair(pairs, (opp_num, our_num, comm), 1.0)

    # Learn the opponent's betting range from hands that reached showdown:
    # what did they show down after betting/raising?
    opp_seat = 1 - req.your_seat
    for action in hand.actions or []:
        if getattr(action, "seat", None) != opp_seat:
            continue
        act = getattr(action, "action", "")
        if act not in ("bet", "raise"):
            continue
        if getattr(action, "round", "") == "pre_reveal":
            _remember(_OPP_PRE_RAISE_CARDS, opp_num)
        elif getattr(action, "round", "") == "post_reveal":
            if act == "raise":
                _remember(_OPP_POST_RAISE_CARDS, opp_num)
            else:
                _remember(_OPP_POST_BET_CARDS, opp_num)


def _hand_hash(hand) -> int:
    shown = tuple(sorted((str(k), str(v)) for k, v in (hand.shown_numbers or {}).items()))
    winners = tuple(sorted(str(w) for w in (hand.winners or [])))
    return hash((hand.hand_number, hand.community_number, hand.pot, shown, winners))


def _record_recent_hands(req: MoveRequest) -> None:
    """Learn from ``recent_hands``, skipping hands we have already seen.

    Deduplication is content-based per (match_id, leg): ``recent_hands``
    overlaps heavily between requests, and ``hand_number`` may be absent in
    some coordinator payloads.
    """
    codename = req.table_rule
    if not codename or codename == "standard":
        return

    key = (req.match_id or "", req.leg_number or 0)
    seen = _SEEN_HANDS.get(key)
    if seen is None:
        seen = set()
        _SEEN_HANDS[key] = seen

    changed = False
    for hand in req.recent_hands or []:
        digest = _hand_hash(hand)
        if digest in seen:
            continue
        seen.add(digest)
        _record_hand(req, codename, hand)
        changed = True

    _SEEN_HANDS.move_to_end(key)
    while len(_SEEN_HANDS) > _SEEN_MAX_KEYS:
        _SEEN_HANDS.popitem(last=False)

    if changed:
        _save_state()


# ---------------------------------------------------------------------------
# Showdown equities
# ---------------------------------------------------------------------------


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


def _rule_showdown(card: int, opp: int, community: int, codename: str) -> float:
    """Showdown share under a codename, using learned powers when available."""
    if codename == "standard":
        return _showdown_value(card, opp, community)

    # Exact recall of an observed matchup wins over any generalization.
    pairs = _RULE_PAIRS.get(codename)
    if pairs:
        value = pairs.get((card, opp, community))
        if value is not None:
            return value

    we_idx = _hand_index(card, card == community)
    opp_idx = _hand_index(opp, opp == community)
    our_power = _codename_power(codename, we_idx)
    opp_power = _codename_power(codename, opp_idx)
    if our_power is None or opp_power is None:
        # No observations for this codename yet: standard rule is the prior.
        return _showdown_value(card, opp, community)

    if our_power > opp_power + _POWER_EPS:
        return 1.0
    if opp_power > our_power + _POWER_EPS:
        return 0.0
    return 0.5


def _equity_pre(card: int, codename: str) -> float:
    if codename == "standard":
        return pre_reveal_equity(card)
    total = 0.0
    for comm in range(1, 14):
        for opp in range(1, 14):
            total += _rule_showdown(card, opp, comm, codename)
    return total / 169.0


def _equity_post(card: int, community: int, codename: str) -> float:
    if codename == "standard":
        return post_reveal_equity(card, community)
    total = 0.0
    for opp in range(1, 14):
        total += _rule_showdown(card, opp, community, codename)
    return total / 13.0


def pot_odds(to_call: int, pot: int) -> float:
    """Fraction of the final pot we must contribute to stay in the hand."""
    if to_call <= 0:
        return 0.0
    return to_call / (pot + to_call)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _range_equity_for(
    card: int, community: int | None, opp_weights: dict[int, float], codename: str
) -> float:
    """Equity vs a weighted opponent-card distribution under a codename.

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
                equity += _rule_showdown(card, opp, comm, codename)
            total += weight * (equity / 13.0)
            weight_sum += weight
        return total / weight_sum if weight_sum else 0.0

    total = 0.0
    weight_sum = 0.0
    for opp, weight in opp_weights.items():
        if weight <= 0:
            continue
        total += weight * _rule_showdown(card, opp, community, codename)
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


def _post_reveal_opp_weights(community: int, raised: bool, codename: str) -> dict[int, float]:
    """Assumed range for a post-reveal bet (raised=False) or raise (raised=True)."""
    weights = {card: 0.0 for card in range(1, 14)}
    for card in range(1, 14):
        if card == community:
            weights[card] = 1.0
            continue
        equity = _equity_post(card, community, codename)
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


def _pre_reveal_opp_weights_observed() -> dict[int, float]:
    """Standard pre-reveal raise range, nudged by observed showdown cards."""
    weights = _pre_reveal_opp_weights()
    for card in _OPP_PRE_RAISE_CARDS:
        weights[card] = weights.get(card, 0.0) + 2.0
    return weights


def _post_reveal_opp_weights_observed(community: int, raised: bool) -> dict[int, float]:
    """Standard post-reveal bet range, nudged by observed showdown cards."""
    weights = _post_reveal_opp_weights(community, raised, "standard")
    observed = _OPP_POST_RAISE_CARDS if raised else _OPP_POST_BET_CARDS
    for card in observed:
        weights[card] = weights.get(card, 0.0) + 2.0
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
    seed = f"{req.match_id}:{req.table_rule}:{req.hand_number}:{req.round}:{key}"
    return zlib.crc32(seed.encode("utf-8")) % 100 < percent


# ---------------------------------------------------------------------------
# Standard-rule strategy
# ---------------------------------------------------------------------------


def _pre_reveal(req: MoveRequest, legal: set[str], codename: str = "standard") -> MoveResponse:
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
    equity = _range_equity_for(card, None, _pre_reveal_opp_weights(), codename)
    odds = pot_odds(to_call, pot)
    if equity > odds + 0.04:
        # Re-raise only the nuts once; keep pots small with everything else.
        if card == 13 and "raise" in legal and to_call < 0.3 * stack and pot < 0.25 * stack:
            return MoveResponse(action="raise", amount=_clamp_raise(req, 0.7))
        return MoveResponse(action="call")
    return MoveResponse(action="fold")


def _post_reveal(req: MoveRequest, legal: set[str], codename: str = "standard") -> MoveResponse:
    card = req.your_number or 1
    community = req.community_number
    to_call = req.to_call or 0
    pot = req.pot or 0
    stack = req.your_stack or 0

    if community is None:
        # Malformed payload guard: treat as pre-reveal.
        return _pre_reveal(req, legal, codename)

    if card == community:
        # Huge favourite: value-bet and never fold.
        if to_call == 0:
            if "bet" in legal:
                return MoveResponse(action="bet", amount=_clamp_raise(req, 0.6))
            return MoveResponse(action="check")
        if "raise" in legal and to_call < 0.5 * stack:
            return MoveResponse(action="raise", amount=_clamp_raise(req, 0.8))
        return MoveResponse(action="call")

    equity = _equity_post(card, community, codename)

    if to_call == 0:
        # First to act post-reveal.
        if "bet" in legal and equity > 0.6:
            return MoveResponse(action="bet", amount=_clamp_raise(req, 0.5))
        if "bet" in legal and equity < 0.3 and _chance(req, "bluff_post", 12):
            return MoveResponse(action="bet", amount=_clamp_raise(req, 0.5))
        return MoveResponse(action="check")

    # Facing a bet/raise: evaluate against the opponent's likely range.
    last_opp = _last_opponent_action(req)
    opp_weights = _post_reveal_opp_weights(community, last_opp == "raise", codename)
    range_equity = _range_equity_for(card, community, opp_weights, codename)
    odds = pot_odds(to_call, pot)

    if range_equity > odds + 0.03:
        # Raise once over a first bet with a very strong non-pair; never
        # re-raise a raise with a non-pair (opponent likely has a pair).
        if (last_opp == "bet" and card >= 12 and community < card
                and "raise" in legal and to_call < 0.25 * stack):
            return MoveResponse(action="raise", amount=_clamp_raise(req, 0.6))
        return MoveResponse(action="call")
    return MoveResponse(action="fold")


# ---------------------------------------------------------------------------
# Unknown-rule (Phase 2) strategy
# ---------------------------------------------------------------------------


def _rule_confidence(obs: int) -> float:
    """0..1 confidence in the learned model for a codename.

    Aggression under an unknown rule scales with this: with few showdowns
    the model is mostly the standard-rule prior, which may be badly wrong
    for the leg's real rule, so we keep pots small until it earns trust.
    """
    return min(1.0, obs / 12.0)


def _rule_pre_reveal(req: MoveRequest, legal: set[str], codename: str) -> MoveResponse:
    """Pre-reveal play under an unknown rule.

    With few observations we keep pots small and call modest raises so hands
    reach showdown and the rule gets learned. Once the codename is understood
    we use the learned equity to value-raise and fold correctly. Pot control
    is capped by confidence so a wrong prior can never cost the stack.
    """
    card = req.your_number or 1
    to_call = req.to_call or 0
    pot = req.pot or 0
    stack = req.your_stack or 0
    obs = _codename_observations(codename)
    conf = _rule_confidence(obs)

    if to_call == 0:
        if obs >= 4 and "raise" in legal:
            equity = _equity_pre(card, codename)
            frac = 0.25 + 0.35 * conf  # 0.25 .. 0.60 pot
            if equity > 0.80 - 0.10 * conf:
                return MoveResponse(action="raise", amount=_clamp_raise(req, frac))
            if equity > 0.55 + 0.25 * (1 - conf):
                return MoveResponse(action="raise", amount=_clamp_raise(req, frac * 0.7))
            if conf >= 0.6 and equity < 0.30 and _chance(req, "bluff_pre_rule", 5):
                return MoveResponse(action="raise", amount=_clamp_raise(req, min(frac, 0.4)))
        return MoveResponse(action="check")

    equity = _range_equity_for(card, None, _pre_reveal_opp_weights_observed(), codename)
    odds = pot_odds(to_call, pot)

    if obs < 4:
        # Exploration: pay small raises to reach showdown and learn the rule.
        if to_call <= max(3, int(0.12 * stack)) and "call" in legal:
            return MoveResponse(action="call")
        if equity > odds + 0.10 and to_call <= int(0.15 * stack) and "call" in legal:
            return MoveResponse(action="call")
        return MoveResponse(action="fold")

    margin = 0.02 + 0.10 * (1 - conf)
    # Never commit more than a fifth of the stack pre-reveal under an
    # uncertain rule unless the learned model is confident we dominate.
    if to_call > int(0.20 * stack) and equity < 0.80:
        return MoveResponse(action="fold")
    if equity > odds + margin:
        if equity > 0.75 and conf >= 0.6 and "raise" in legal and to_call < 0.15 * stack:
            return MoveResponse(action="raise", amount=_clamp_raise(req, 0.25 + 0.30 * conf))
        return MoveResponse(action="call")
    return MoveResponse(action="fold")


def _rule_post_reveal(req: MoveRequest, legal: set[str], codename: str) -> MoveResponse:
    """Post-reveal play under an unknown rule using learned equities.

    Sizing and the threshold for betting rise with confidence in the learned
    model. Calls are hard-capped by stack fraction so a misread rule can bleed
    small pots but can never stack us in one hand.
    """
    card = req.your_number or 1
    community = req.community_number
    if community is None:
        return _rule_pre_reveal(req, legal, codename)

    to_call = req.to_call or 0
    pot = req.pot or 0
    stack = req.your_stack or 0
    obs = _codename_observations(codename)
    conf = _rule_confidence(obs)
    equity = _equity_post(card, community, codename)

    if to_call == 0:
        if obs >= 3 and "bet" in legal:
            frac = 0.30 + 0.40 * conf  # 0.30 .. 0.70 pot
            if equity > 0.90 - 0.35 * conf:
                return MoveResponse(action="bet", amount=_clamp_raise(req, frac))
            if conf >= 0.6 and equity < 0.30 and _chance(req, "bluff_post_rule", 5):
                return MoveResponse(action="bet", amount=_clamp_raise(req, min(frac, 0.4)))
        return MoveResponse(action="check")

    last_opp = _last_opponent_action(req)
    # The opponent plays the same way in every leg, regardless of the rule, so
    # estimate THEIR range with the standard model plus showdown observations,
    # while evaluating OUR hand with the learned rule.
    opp_weights = _post_reveal_opp_weights_observed(community, last_opp == "raise")
    range_equity = _range_equity_for(card, community, opp_weights, codename)
    odds = pot_odds(to_call, pot)
    margin = 0.02 + 0.10 * (1 - conf)

    # Pot control: never commit more than a third of the stack on one call
    # unless the learned model is confident we are a big favourite.
    if to_call > int(0.35 * stack):
        if equity > 0.78 and conf >= 0.4 and "call" in legal:
            return MoveResponse(action="call")
        return MoveResponse(action="fold")

    if equity > 0.70 and "call" in legal:
        if (last_opp == "bet" and equity > 0.72 and conf >= 0.6
                and "raise" in legal and to_call < 0.25 * stack):
            return MoveResponse(action="raise", amount=_clamp_raise(req, 0.30 + 0.30 * conf))
        return MoveResponse(action="call")

    if obs < 4 and to_call <= max(3, int(0.10 * stack)) and "call" in legal:
        return MoveResponse(action="call")

    if to_call > int(0.15 * stack) and equity < 0.55:
        return MoveResponse(action="fold")

    if range_equity > odds + margin:
        return MoveResponse(action="call")
    return MoveResponse(action="fold")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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
    """Pick the bot's next action for a /move request.

    ``table_rule`` is read from every request. Unknown codenames are played
    with the learned adaptive strategy; observations from ``recent_hands`` are
    folded into the model first so decisions use the freshest data.
    """
    legal = set(req.legal_actions or [])
    _record_recent_hands(req)

    codename = req.table_rule or "standard"
    if codename in KNOWN_TABLE_RULES:
        if req.round == "pre_reveal":
            resp = _pre_reveal(req, legal, codename)
        else:
            resp = _post_reveal(req, legal, codename)
    else:
        if req.round == "pre_reveal":
            resp = _rule_pre_reveal(req, legal, codename)
        else:
            resp = _rule_post_reveal(req, legal, codename)

    return _legalize(resp, req, legal)


# Restore any models learned in earlier attempts at import time. Leg order
# and per-leg rules are identical on every retry, so state from a previous
# attempt is still valid for the codenames it describes.
_load_state()
