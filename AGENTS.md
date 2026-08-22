# Repository Guidelines

Production-grade FastAPI service for the UBS Singapore Global Coding Challenge, managed with `uv`.

## Competition Briefing (UBS Global Coding Challenge)

- **Event**: In-person coding challenge on **Saturday, 22 August 2026**; online pre-event session on **Friday, 21 August 2026, 17:30–18:30 SGT**.
- **Attendance**: All participants must attend in-person at the designated venues. Seek clarifications from the on-site challenge developers.

### Saturday Schedule (SGT)

| Time | Activity |
| --- | --- |
| 08:00–08:30 | Event Introduction and Registration (breakfast at own time) |
| 08:30–11:30 | Batch 1 Challenges |
| 11:30–14:30 | Batch 2 Challenges (lunch at own time) |
| 14:30–17:30 | Batch 3 Challenges (tea break at own time) |
| 17:30–18:00 | Introduction to Junior Talent Programs / Networking with Senior Leaders |
| 18:00–18:30 | Prize Presentation and Event Closing |

- During networking, staff may ask about your code; interpersonal skills count toward performance.

### Getting Started

- Bring your own laptop with the development environment for your chosen language(s); any language supported by your cloud platform is fine, and mixing languages/platforms is allowed.
- Install a **Git client** for version control.
- Use a cloud application platform (Render recommended) for deployment; free tiers only — do not pay for higher capacity or resources.
- Challenges interact with your app **over web APIs**; expose the endpoint/path requested in each challenge's instructions.
- Official boilerplates are available for Kotlin (Spring Boot), Node.js, and Python.

### Competition Workflow

1. Pick a challenge and read its instructions thoroughly.
2. Build the solution and deploy it to your cloud platform (Render deployments are in Europe/US — account for latency on large file uploads/downloads).
3. Add your server/app URL on the **Server Configuration** page.
4. Submit an **evaluation** for the challenge/server pair.
5. Repeat for more challenges.

### Leaderboard

- Ranked by **total score across all challenges**; ties are broken by **ascending completion time**.

## SHOWDOWN Challenge

Heads-up no-limit betting game between bots. We expose one HTTP endpoint; the coordinator deals, runs the betting, and calls us whenever it is our turn.

### Endpoints

- `POST /move` — called on our turn. Reply within 5 seconds with HTTP 200 and `{"action": "check" | "call" | "bet" | "raise" | "fold", "amount": <int>}`. `amount` is required only for `bet`/`raise` and is the **total for the current betting round**; it must fall inside `[min_raise_to, max_raise_to]`.
- `GET /health` (and `/healthz`) — warm-up probe; return HTTP 200.

### Game rules (standard table)

- Each hand: forced bets (blinds 1 and 2, alternating with the button), deal one secret number 1–13 to each player, `pre_reveal` betting round, reveal one community number 1–13, `post_reveal` betting round, showdown.
- Showdown: a **pair** (your number == community number) beats any non-pair; otherwise higher number wins; identical results split the pot. Fold wins immediately and nothing is revealed.
- No-limit: stack is the only ceiling. Scoring is by chip delta (start 200 per match); busting at 0 locks in −200.
- `legal_actions` on every request is authoritative — always reply with one of them. `players` is a list in seat order and always contains us under the name `"you"`; ignore unknown fields (the coordinator adds fields over the event and never removes them).
- Position: the button pays the small blind and acts first `pre_reveal`; the order flips after the reveal. We never need to compute order ourselves, but `button_seat` indicates which side we are on.
- Five consecutive bad responses forfeit the match; the coordinator never retries, so `/move` must stay fast and side-effect-free.

### Bot strategy (`app/strategy.py`)

- `pre_reveal_equity(card)` = `(22 * card + 15) / 338` vs a random opponent.
- `post_reveal_equity(card, community)` = `25/26` for a pair; otherwise `(wins + 0.5 ties) / 13`.
- Range-aware decisions: when facing bets/raises, judge our hand against an estimated opponent range instead of a random hand; value-raise strong cards, fold weak hands to big bets, occasional deterministic bluffs, never fold a pair, and never re-raise a non-pair into a raise post-reveal.
- Unknown `table_rule` values fall back to a conservative check/call-small/fold strategy. Phase guides add twists on top of this page's rules.

## Ghost Chains Challenge

Real-time AML transaction risk scoring over a rolling 24-hour directed graph.

- `GET /ghost-chains/health` → `{"status": "ok"}`.
- `POST /ghost-chains/reset` — clears all graph/idempotency state.
- `POST /ghost-chains/transactions` — scores each transaction in `[0, 1]` sequentially, preserving order.

`app/ghost_chains.py` keeps the stateful engine: transitive reachability closure over active edges, incremental edge insertion, expiry of edges older than 24h (boundary inclusive), idempotency by `txId`, and a structural raw score = `new_paths + 2*parallel_paths + 10*cycle + 15*multi_loop_nodes`, mapped monotonically to `[0,1]`.

Phase 1 scores only the structural signal: extension < convergence < single return < multi-loop return. Phases 2 and 3 will layer identity (ip/device) and amount-trail signals on top; optionals are stored but ignored for now.

## Tool-box / Nursery Challenge

Multi-turn agent with tool use connects to our **MCP server at `/mcp`** (Streamable HTTP). We decide tool names, schemas, and outputs.

- `GET /mcp` — SSE endpoint event for the GET-first handshake, or JSON info.
- `POST /mcp` — JSON-RPC 2.0 (`initialize`, `tools/list`, `tools/call`, notifications → 202). SSE-encoded responses when the client sends `Accept: text/event-stream`.

Tools in `app/mcp_server2.py` (FastMCP from the `mcp` library, Streamable HTTP; mounted at `/mcp` in `app/main.py`, whose lifespan runs the MCP app's lifespan explicitly since FastAPI does not propagate lifespans into mounts):

| Tool | Purpose | Returns |
| --- | --- | --- |
| `get_name` | "What is your name?" | `Nursery` (string, 3–30 chars) |
| `calculate` | full arithmetic expression, e.g. `2 + 3 * 5` | exact number with correct `* /` before `+ -` precedence |
| `classify_shape` | base64 PNG | `rectangle`, `triangle`, or `circle` (OpenCV contours + circularity) |
| `recall` / `retrieve` | RAG over the official study materials: hashed word + char-trigram TF-IDF embeddings, cosine retrieval + paraphrase-bridge rerank; fetches `/study-materials` automatically | list of passages, ≤ 900 o200k tokens, runner-up doc always included |
| `navigate` | next node on the least-cost route (`GET /graph?map_id=…`) | next node label |
| `route` | full least-cost node path | `A -> B -> D \| total cost …` |
| `find_open_venues` / `venues_open` / `where_to_eat` | venues open at a time on a day (`GET /venues/{day}`) | comma-separated names |
| `find_meeting_window` / `meeting_window` | clean-window-first meeting time (`GET /schedule/{person}/{day}` + own busy/tentative/inbox) | `HH:MM-HH:MM` |
| `find_meeting_point` / `meeting_point` | median grid point minimising total Manhattan travel (`GET /location/{person}/{day}`) | `[x, y]` |
| `plan_outing` / `outing` | meeting window + meeting point + place to eat, whole journey minimised | window, point, venue |

## Project Structure & Module Organization

- `app/` — FastAPI source; `app/main.py` defines the app, routes, and middleware.
- `app/models.py` — Pydantic request/response schemas for all challenge endpoints.
- `app/strategy.py` — pure decision logic for the SHOWDOWN betting bot (equity, pot odds, legal-action guard).
- `app/ghost_chains.py` — stateful scoring engine for the Ghost Chains AML challenge.
- `app/mcp_server2.py` — MCP server (JSON-RPC over Streamable HTTP via FastMCP) for the Tool-box / Nursery challenge: phase 1 tools, RAG recall, graph navigation, and the phase 3 city-and-clock tools.
- `app/routers/` — APIRouters for `/solve`, `/move`, `/ghost-chains/*`.
- `tests/` — pytest suite; `tests/test_main.py` covers the API, `tests/test_strategy.py` covers strategy logic, `tests/test_mcp.py` covers the MCP transport, `tests/test_toolbox.py` covers the RAG recall and phase 3 tools, `tests/test_ghost_chains.py` covers Ghost Chains.
- `main.py` — root entry-point stub.
- `pyproject.toml` — project metadata, dependencies, and uv config.
- `render.yaml` — Render web service deployment config.
- `.agents/` — agent skills (smoke, verify, phase-tag) and MCP config.
- `AGENTS.md` — role, scope, and competition briefing for coding agents.

## Build, Test, and Development Commands

- `uv sync` — install project and dev dependencies.
- `uv run fastapi dev app/main.py --host 0.0.0.0 --port 8000` — run the dev server with reload.
- `uv run fastapi run app/main.py --host 0.0.0.0 --port $PORT` — production start (used by Render).
- `uv run pytest -v` — run the full test suite.
- `uv run ruff check .` — lint the codebase.

## Coding Style & Naming Conventions

- Python 3.12; `snake_case` for functions/variables, `PascalCase` for Pydantic models.
- Define explicit request/response schemas with `Field` constraints and `@field_validator`.
- Map expected edge cases to `HTTPException`; rely on the global exception handler to avoid unhandled 500s.
- Format and lint with Ruff.

## Testing Guidelines

- pytest with FastAPI's `TestClient`.
- Place tests in `tests/`, named `test_*.py` with `test_*` functions.
- Every business-logic function needs unit and integration coverage; run `uv run pytest -v` and pass before marking work complete.

## Commit & Pull Request Guidelines

- Keep commits small and focused; finalize each phase with the `phase-tag` skill (defaults: message "Phase complete", tag `v1.0.0-phase`).
- PRs must describe changes and preserve backward compatibility with earlier phases — never break existing routes, contracts, or schemas.

## Agent-Specific Instructions

- `.agents/skills/`: `smoke` hits `/healthz`, `verify` runs Ruff + pytest, `phase-tag` commits and tags a phase.
- Security: enforce CORS, sanitize inputs, restrict payload sizes, and avoid hardcoded secrets.
