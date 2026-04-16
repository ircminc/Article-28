#!/usr/bin/env bash
# Start backend + frontend together in a Codespace.
# Logs interleave to stdout; Ctrl-C stops both.
set -euo pipefail

trap 'echo ""; echo "Stopping..."; kill 0 2>/dev/null || true; exit 0' INT TERM

echo "› backend  : http://localhost:8000  (forwarded to Codespaces URL)"
echo "› frontend : http://localhost:3000  (open via Ports panel)"
echo ""

# Start backend
(uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload 2>&1 | sed -u 's/^/[backend] /') &

# Small pause so the backend binds first and the frontend proxy works from t=0
sleep 1.5

# Start frontend
(cd frontend && npm run dev -- --host 0.0.0.0 2>&1 | sed -u 's/^/[frontend] /') &

wait
