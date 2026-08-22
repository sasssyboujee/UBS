Phase 2 — Reading the Table
The full game rules and protocol are on the SHOWDOWN guide. This page covers what phase 2 adds: table rules, and attempts made of several legs.

Table rules
Every match is played under one table rule — a modification to how the showdown is decided. It is fixed for the whole match and announced in table_rule on every request. Only the showdown changes: betting, forced bets, position and sizing are identical under every rule.

We are not telling you what the rules are, or how many there are.

Here is a rule that is not in play, so you know the shape of the thing:

Odd numbers beat even numbers; within each group, higher still wins.

That is an illustration of the kind of change a rule can make — nothing more. The real rules are not this one. Do not code against it.

table_rule is a codename
table_rule carries an opaque string — something like chalcedony, also not a real one. It identifies the ruleset without describing it.

The mapping is fixed for the whole event: the same codename always means the same ruleset, in every match, every attempt, and every later phase.

Read it on every request rather than assuming it carries over. The same number can be a monster under one rule and worthless under another.

Legs
An attempt is four legs played back to back. Each leg is a complete match with fresh 200-chip stacks and its own table rule — hand_number restarts at 1 and every chip_delta restarts at 0. leg_number and total_legs tell you where you are; both are null in a single-match phase.

recent_hands does not carry across legs. It resets when a new leg starts.

Phase 2: Reading the Table — 400 pts
Four legs, 40 hands each, a different table rule on each.

The same opponent plays all four legs, under the same name, and plays the same way throughout. Their name is drawn fresh each attempt, so it never means anything.

The leg order and each leg's rule are identical on every retry — only the cards change.

Per leg: chip delta ≥ +25 → 100 points. Points accumulate per leg; you don't need all four to score. All four is the full 400.

Glossary additions
Terms added to the SHOWDOWN guide's glossary:

Term	Meaning
Leg	One complete match inside a multi-match attempt. Fresh stacks, its own rule, its own recent_hands.
Table rule (table_rule)	The showdown ruleset a match is played under. Announced on every request as a codename, and never changes mid-match.
Codename	The opaque string table_rule carries. It identifies a ruleset without describing it; the same codename always means the same ruleset.
