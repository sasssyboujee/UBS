#!/bin/bash
set -e
echo "Running Ruff checks..."
uv run ruff check .
echo "Running Pytest..."
uv run pytest -v
