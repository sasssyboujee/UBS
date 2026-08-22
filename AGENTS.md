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

## Project Structure & Module Organization

- `app/` — FastAPI source; `app/main.py` defines the app, routes, Pydantic models, and middleware.
- `tests/` — pytest suite; `tests/test_main.py` covers the API.
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
