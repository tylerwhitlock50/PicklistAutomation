#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p data logs exports

# Podman/Docker bind mounts can misbehave if a directory exists where a DB file is expected.
if [[ -d "picklist_history.db" ]]; then
  echo "Found legacy directory 'picklist_history.db'."
  echo "Database now uses 'data/picklist_history.db'. Keeping legacy directory unchanged."
fi

if [[ -f "picklist_history.db" && ! -f "data/picklist_history.db" ]]; then
  cp "picklist_history.db" "data/picklist_history.db"
  echo "Copied legacy DB file to data/picklist_history.db"
fi

echo "Setup complete."
echo "Created/verified: data/, logs/, exports/"
