#!/bin/bash
set -e
echo "Hitting http://localhost:8000/healthz..."
curl -sSf http://localhost:8000/healthz
echo -e "\nSmoke test passed!"
