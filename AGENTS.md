# Repository Guidelines

Production-grade FastAPI service for the UBS Singapore Global Coding Challenge, managed with `uv`.

## Project Structure & Module Organization

- `app/` — FastAPI source; `app/main.py` defines the app, routes, Pydantic models, and middleware.
- `tests/` — pytest suite; `tests/test_main.py` covers the API.
- `main.py` — root entry-point stub.
- `pyproject.toml` — project metadata, dependencies, and uv config.
- `render.yaml` — Render web service deployment config.
- `.agents/` — agent skills (smoke, verify, phase-tag) and MCP config.
- `agent.md` — role and scope for coding agents.

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
