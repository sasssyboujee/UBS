"""Heads-up SHOWDOWN simulator: stress-test the betting strategy under
hostile unknown table rules against a fixed, standard-playing opponent.

The phase-2 coordinator runs four 40-hand legs per attempt, each under a
different opaque table rule, against one opponent who plays the same way in
every leg. This harness replays that setup with a Nadia-like opponent and
compares the current strategy with a saved pre-change copy.

Usage:
    uv run python scripts/sim_showdown.py [--attempts 40] [--seed 1]
"""

from __future__ import annotations

import argparse
import importlib.util
import random

from app import strategy as new_strategy
from app.models import MoveRequest

OLD_PATH = "/tmp/strategy_old_showdown.py"

# ---------------------------------------------------------------------------
# Showdown rules (the bot never sees these; it only sees the opaque codename)
# ---------------------------------------------------------------------------


def rule_standard(our: int, opp: int, comm: int) -> float:
    if our == comm and opp == comm:
        return 0.5
    if our == comm:
        return 1.0
    if opp == comm:
        return 0.0
    if our > opp:
        return 1.0
    if our < opp:
        return 0.0
    return 0.5


def rule_low_wins(our: int, opp: int, comm: int) -> float:
    # Pairs still beat non-pairs; among non-pairs the LOWER card wins.
    if our == comm and opp == comm:
        return 0.5
    if our == comm:
        return 1.0
    if opp == comm:
        return 0.0
    if our < opp:
        return 1.0
    if our > opp:
        return 0.0
    return 0.5


def rule_pair_loses(our: int, opp: int, comm: int) -> float:
    # A pair is the WORST hand; otherwise high card wins.
    if our == comm and opp == comm:
        return 0.5
    if our == comm:
        return 0.0
    if opp == comm:
        return 1.0
    if our > opp:
        return 1.0
    if our < opp:
        return 0.0
    return 0.5


def rule_low_pair_loses(our: int, opp: int, comm: int) -> float:
    # Pair loses to any non-pair; among non-pairs the LOWEST card wins.
    if our == comm and opp == comm:
        return 0.5
    if our == comm:
        return 0.0
    if opp == comm:
        return 1.0
    if our < opp:
        return 1.0
    if our > opp:
        return 0.0
    return 0.5


LEGS = [
    ("opaque_standard", rule_standard),
    ("low_wins", rule_low_wins),
    ("pair_loses", rule_pair_loses),
    ("low_pair_loses", rule_low_pair_loses),
]


# ---------------------------------------------------------------------------
# The opponent: standard-equity, rule-agnostic, mildly station-y
# ---------------------------------------------------------------------------


def pre_eq(card: int) -> float:
    return (22 * card + 15) / 338.0


def post_eq(card: int, comm: int) -> float:
    if card == comm:
        return 25.0 / 26.0
    return ((card - 1) - (1 if comm < card else 0) + 0.5) / 13.0


def nadia_act(req: MoveRequest):
    """Nadia's decision for the acting seat described by ``req``.

    A loose, station-y standard bot: pays off value bets, rarely folds to
    small bets, but only raises with strong standard hands. It plays the
    same way under every table rule.
    """
    card = req.your_number or 1
    comm = req.community_number
    to_call = req.to_call or 0
    pot = req.pot or 0
    legal = set(req.legal_actions or [])
    our_bet = 0
    for player in req.players:
        if player.seat == req.your_seat:
            our_bet = player.bet_this_round

    def amount_for(target: int):
        if req.min_raise_to is None or req.max_raise_to is None:
            return None
        return max(req.min_raise_to, min(target, req.max_raise_to))

    if req.round == "pre_reveal":
        if to_call == 0:
            if card >= 11 and "raise" in legal:
                return ("raise", amount_for(our_bet + max(4, pot // 2)))
            if card == 10 and "raise" in legal:
                return ("raise", amount_for(our_bet + 4))
            return ("check", None)
        if to_call == 1 and card >= 3:
            return ("call", None)
        if card >= 13 and "raise" in legal and to_call < 0.30 * (req.your_stack or 0):
            return ("raise", amount_for(our_bet + 2 * to_call))
        if pre_eq(card) >= to_call / (pot + to_call) - 0.05 or to_call <= 4:
            return ("call", None)
        return ("fold", None)

    if to_call == 0:
        if card == comm and "bet" in legal:
            return ("bet", amount_for(pot * 3 // 5))
        if post_eq(card, comm) > 0.65 and "bet" in legal:
            return ("bet", amount_for(pot // 2))
        return ("check", None)
    if card == comm and "raise" in legal:
        return ("raise", amount_for(our_bet + 2 * to_call))
    if post_eq(card, comm) > 0.35 or to_call <= 3:
        return ("call", None)
    return ("fold", None)


# ---------------------------------------------------------------------------
# Game engine (protocol-faithful heads-up no-limit)
# ---------------------------------------------------------------------------


class Leg:
    SB = 1
    BB = 2

    def __init__(self, choose, rule, codename, match_id, leg_number, rng):
        self.choose = choose
        self.rule = rule
        self.codename = codename
        self.match_id = match_id
        self.leg_number = leg_number
        self.rng = rng
        self.stacks = {0: 200, 1: 200}
        self.button = 0
        self.hands: list[dict] = []
        self.current_log: list[dict] = []
        self.illegal_responses = {0: 0, 1: 0}

    def build_request(self, hand_no, round_label, actor, cards, comm, stacks,
                      bets, max_bet, last_inc, pot, to_call) -> MoveRequest:
        legal = []
        min_r = max_r = None
        if to_call > 0:
            legal += ["fold", "call"]
            min_r = max_bet + last_inc
            max_r = bets[actor] + stacks[actor]
            if max_r > min_r:
                legal.append("raise")
        else:
            legal.append("check")
            if stacks[actor] > 0:
                min_r = self.BB
                max_r = bets[actor] + stacks[actor]
                legal.append("bet")
        return MoveRequest(
            protocol_version=2,
            match_id=self.match_id,
            phase=2,
            table_rule=self.codename,
            small_blind=self.SB,
            big_blind=self.BB,
            starting_stack=200,
            your_stack=stacks[actor],
            hand_number=hand_no,
            total_hands=40,
            round=round_label,
            leg_number=self.leg_number,
            total_legs=4,
            your_number=cards[actor],
            community_number=comm,
            your_seat=actor,
            button_seat=self.button,
            pot=pot,
            to_call=to_call,
            min_raise_to=min_r,
            max_raise_to=max_r,
            legal_actions=legal,
            players=[
                {"seat": 0, "name": "you", "bet_this_round": bets[0], "stack": stacks[0]},
                {"seat": 1, "name": "Nadia", "bet_this_round": bets[1], "stack": stacks[1]},
            ],
            current_hand_actions=self.current_log,
            recent_hands=self.hands,
        )

    def act(self, actor, req: MoveRequest):
        if actor == 0:
            resp = self.choose(req)
            action, amount = resp.action, resp.amount
        else:
            action, amount = nadia_act(req)

        # Enforce legality the way the coordinator would (and count how often
        # a strategy submits something the coordinator would reject).
        legal = set(req.legal_actions or [])
        if action not in legal:
            self.illegal_responses[actor] += 1
            action = "call" if req.to_call and "call" in legal else "check"
            if action not in legal:
                action = "fold"
            amount = None
        if action in ("bet", "raise"):
            if amount is None or req.min_raise_to is None or req.max_raise_to is None:
                self.illegal_responses[actor] += 1
                action = "call" if req.to_call and "call" in legal else "check"
                amount = None
            else:
                amount = int(amount)
                if amount < req.min_raise_to or amount > req.max_raise_to:
                    self.illegal_responses[actor] += 1
                    amount = max(req.min_raise_to, min(amount, req.max_raise_to))
        return action, amount

    def betting_round(self, hand_no, round_label, first, posted, cards, comm,
                      stacks, pot):
        bets = {0: posted.get(0, 0), 1: posted.get(1, 0)}
        max_bet = max(bets.values())
        last_inc = self.BB
        to_act = first
        acted = {0: False, 1: False}

        while True:
            actor = to_act
            to_call = max_bet - bets[actor]
            req = self.build_request(
                hand_no, round_label, actor, cards, comm, stacks,
                bets, max_bet, last_inc, pot, to_call,
            )
            action, amount = self.act(actor, req)
            self.current_log.append(
                {"round": round_label, "seat": actor, "action": action, "amount": amount}
            )

            if action == "fold":
                return ("fold", actor), pot
            if action == "check":
                pass
            elif action == "call":
                pay = min(to_call, stacks[actor])
                stacks[actor] -= pay
                bets[actor] += pay
                pot += pay
            else:  # bet / raise: amount is the total for this betting round
                pay = min(amount - bets[actor], stacks[actor])
                target = bets[actor] + pay
                stacks[actor] -= pay
                pot += pay
                last_inc = target - max_bet
                max_bet = target
                bets[actor] = target
                acted[1 - actor] = False
            acted[actor] = True

            if stacks[actor] == 0:
                # All-in: the other player still gets one chance to respond.
                if acted[1 - actor]:
                    return None, pot
                to_act = 1 - actor
                continue
            if acted[0] and acted[1] and bets[0] == bets[1]:
                return None, pot
            to_act = 1 - actor

    def settle_showdown(self, hand_no, cards, comm, stacks, pot):
        share = self.rule(cards[0], cards[1], comm)
        if share == 1.0:
            winners = [0]
        elif share == 0.0:
            winners = [1]
        else:
            winners = [0, 1]
        self.hands.append(
            {
                "hand_number": hand_no,
                "community_number": comm,
                "winners": winners,
                "pot": pot,
                "shown_numbers": {"you": cards[0], "Nadia": cards[1]},
                "actions": list(self.current_log),
            }
        )
        if len(winners) == 1:
            stacks[winners[0]] += pot
        else:
            half = pot // 2
            stacks[0] += pot - half
            stacks[1] += half

    def play_hand(self, hand_no):
        btn = self.button
        sb, bb = btn, 1 - btn
        cards = {0: self.rng.randint(1, 13), 1: self.rng.randint(1, 13)}
        comm = self.rng.randint(1, 13)
        stacks = self.stacks
        # Blinds: a player who cannot cover the blind posts what they have
        # (all-in blind); stacks can never go negative.
        posted = {sb: min(1, stacks[sb]), bb: min(2, stacks[bb])}
        stacks[sb] -= posted[sb]
        stacks[bb] -= posted[bb]
        pot = posted[sb] + posted[bb]
        self.current_log = []
        assert sum(stacks.values()) + pot == 400, (hand_no, stacks, pot)

        pre = self.betting_round(
            hand_no, "pre_reveal", sb, posted, cards, None, stacks, pot
        )
        if pre is not None:
            # Pre-reveal fold: nothing is revealed.
            _, loser = pre
            winner = 1 - loser
            stacks[winner] += pot
            self.hands.append(
                {
                    "hand_number": hand_no,
                    "community_number": None,
                    "winners": [winner],
                    "pot": pot,
                    "shown_numbers": {},
                    "actions": list(self.current_log),
                }
            )
            return

        if stacks[0] > 0 and stacks[1] > 0:
            post = self.betting_round(
                hand_no, "post_reveal", bb, {}, cards, comm, stacks, pot
            )
            if post is not None:
                _, loser = post
                winner = 1 - loser
                stacks[winner] += pot
                self.hands.append(
                    {
                        "hand_number": hand_no,
                        "community_number": comm,
                        "winners": [winner],
                        "pot": pot,
                        "shown_numbers": {},
                        "actions": list(self.current_log),
                    }
                )
                return

        self.settle_showdown(hand_no, cards, comm, stacks, pot)

    def play_leg(self, hands=40):
        for hand_no in range(1, hands + 1):
            self.play_hand(hand_no)
            if self.stacks[0] <= 0 or self.stacks[1] <= 0:
                break
            self.button = 1 - self.button
        return self.stacks[0] - 200


# ---------------------------------------------------------------------------
# Attempt runner + reporting
# ---------------------------------------------------------------------------


def load_old():
    spec = importlib.util.spec_from_file_location("strategy_old_showdown", OLD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_attempts(choose, reset, attempts, base_seed):
    per_leg = {i: [] for i in range(4)}
    busts = [0] * 4
    clears = [0] * 4
    illegal = [0, 0]
    for attempt in range(attempts):
        reset()
        match_id = f"sim-{base_seed}-{attempt}"
        rng = random.Random(base_seed * 1_000_003 + attempt)
        for leg_idx, (codename, rule) in enumerate(LEGS):
            leg = Leg(choose, rule, codename, match_id, leg_idx + 1, rng)
            delta = leg.play_leg(40)
            per_leg[leg_idx].append(delta)
            if delta == -200:
                busts[leg_idx] += 1
            if delta >= 25:
                clears[leg_idx] += 1
            illegal[0] += leg.illegal_responses[0]
            illegal[1] += leg.illegal_responses[1]
    return per_leg, busts, clears, illegal


def report(label, attempts, per_leg, busts, clears, illegal):
    print(f"\n== {label} ({attempts} attempts x 4 legs) ==")
    for i, (codename, _rule) in enumerate(LEGS):
        deltas = per_leg[i]
        mean = sum(deltas) / len(deltas)
        print(
            f"  leg {i + 1} {codename:<16} mean {mean:+8.1f}  "
            f"min {min(deltas):+5d}  max {max(deltas):+5d}  "
            f"busts {busts[i]:>2}/{attempts}  cleared(>=+25) {clears[i]:>2}/{attempts}"
        )
    all_four = sum(
        1
        for a in range(attempts)
        if all(per_leg[i][a] >= 25 for i in range(4))
    )
    print(
        f"  illegal responses submitted: ours {illegal[0]}, "
        f"opponent {illegal[1]}"
    )
    print(f"  attempts clearing all 4 legs: {all_four}/{attempts}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=30)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    old = load_old()

    print(f"seed {args.seed}, {args.attempts} attempts per strategy")
    report(
        "OLD strategy (pre-change)",
        args.attempts,
        *run_attempts(old.choose_action, old._reset_learning, args.attempts, args.seed)[:4],
    )
    report(
        "NEW strategy (post-change)",
        args.attempts,
        *run_attempts(
            new_strategy.choose_action,
            new_strategy._reset_learning,
            args.attempts,
            args.seed,
        )[:4],
    )


if __name__ == "__main__":
    main()
