#!/usr/bin/env bash
# Build and start the app with the VISUAL document share mounted (see docker-compose.documents.yml).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker compose -f docker-compose.yml -f docker-compose.documents.yml up --build -d
