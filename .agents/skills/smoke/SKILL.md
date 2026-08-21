---
name: smoke
description: >-
  Use this skill to perform a smoke test against the local FastAPI server by hitting the healthz endpoint.
---

# Smoke Test Skill

This skill validates that the local FastAPI server is running and healthy.

## Steps

1. Ensure the server is running locally on port 8000.
2. Run the smoke test script:
   [run.sh](./scripts/run.sh)
3. Validate that a 200 OK status is returned.
