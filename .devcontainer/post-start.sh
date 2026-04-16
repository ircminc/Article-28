#!/usr/bin/env bash
# Runs every time the Codespace starts (including cold-boot wake-ups).
# Cheap and idempotent — just prints a reminder of how to start the stack.
set -euo pipefail

cat <<'BANNER'

╭──────────────────────────────────────────────────────────────────╮
│  Codespace ready. To start the app:                                │
│    bash .devcontainer/start-dev.sh                                 │
│                                                                    │
│  Or in two separate terminals (for live logs):                     │
│    Terminal 1:  uvicorn backend.main:app --reload --port 8000      │
│    Terminal 2:  cd frontend && npm run dev                         │
╰──────────────────────────────────────────────────────────────────╯
BANNER
