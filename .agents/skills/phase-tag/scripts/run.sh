#!/bin/bash
set -e

PHASE_MSG=${1:-"Phase complete"}
TAG_VER=${2:-"v1.0.0-phase"}

echo "Stashing any untracked or uncommitted work not staged..."
git stash -k -u || echo "No unstaged changes to stash"

echo "Committing staged phase changes..."
git commit -m "$PHASE_MSG" || echo "Nothing to commit"

echo "Tagging commit as $TAG_VER..."
git tag -a "$TAG_VER" -m "$PHASE_MSG" -f

echo "Phase tagged successfully."
