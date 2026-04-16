#!/usr/bin/env bash
# One-time setup when a Codespace is first created.
# Installs dependencies, seeds a synthetic dataset so the app is usable out
# of the box, and bootstraps the initial admin from Codespaces secrets.
set -euo pipefail

echo ""
echo "╭──────────────────────────────────────────────────────────────────╮"
echo "│  APG Analyzer — first-time Codespace setup                        │"
echo "╰──────────────────────────────────────────────────────────────────╯"

# ---------------------------------------------------------------------------
# 1. Backend deps
# ---------------------------------------------------------------------------
echo ""
echo "[1/5] Installing backend (Python) dependencies..."
python -m pip install --upgrade pip --quiet
python -m pip install -r backend/requirements.txt --quiet
echo "      ✔ backend deps installed"

# ---------------------------------------------------------------------------
# 2. Frontend deps
# ---------------------------------------------------------------------------
echo ""
echo "[2/5] Installing frontend (Node) dependencies..."
cd frontend
npm ci --no-audit --no-fund --loglevel=error
cd ..
echo "      ✔ frontend deps installed"

# ---------------------------------------------------------------------------
# 3. Reference data — real workbook if mounted, else synthetic seed
# ---------------------------------------------------------------------------
echo ""
echo "[3/5] Loading reference data..."
mkdir -p backend/data workbooks

REAL_XLSX=$(ls -1 workbooks/*.xlsx 2>/dev/null | head -n 1 || true)
if [[ -n "$REAL_XLSX" ]]; then
  echo "      Detected real workbook: $REAL_XLSX"
  python -m backend.db.init_db --workbook "$REAL_XLSX"
else
  echo "      No workbook in workbooks/ — seeding SYNTHETIC reference data."
  echo "      (App will run with fake EAPGs/rates; good enough for demos.)"
  python -m backend.db.seed_synthetic
fi
echo "      ✔ reference data loaded"

# ---------------------------------------------------------------------------
# 4. Initial admin
# ---------------------------------------------------------------------------
echo ""
echo "[4/5] Bootstrapping initial admin..."
if [[ -z "${APP_ADMIN_USERNAME:-}" || -z "${APP_ADMIN_PASSWORD:-}" ]]; then
  echo "      ⚠ APP_ADMIN_USERNAME or APP_ADMIN_PASSWORD not set."
  echo "        Add them as Codespaces secrets:"
  echo "          GitHub → Settings → Codespaces → Repository secrets"
  echo "        Then rebuild this Codespace (Ctrl-Shift-P → Codespaces: Rebuild Container)."
else
  python -m backend.db.init_admin || true
  echo "      ✔ admin ready (username: ${APP_ADMIN_USERNAME})"
fi

# ---------------------------------------------------------------------------
# 5. Done — print next-step instructions
# ---------------------------------------------------------------------------
cat <<'BANNER'

╭──────────────────────────────────────────────────────────────────╮
│  Ready.                                                           │
│                                                                    │
│  To start the app:                                                 │
│    1. Open the "Ports" panel in VS Code (bottom tray)             │
│    2. Right-click port 3000 → Port Visibility → Public            │
│    3. Right-click port 8000 → Port Visibility → Public            │
│    4. Run:  bash .devcontainer/start-dev.sh                        │
│    5. Click the 🌐 next to port 3000 to open the app              │
│                                                                    │
│  Sign in with:                                                     │
│    username: APP_ADMIN_USERNAME (Codespaces secret)                │
│    password: APP_ADMIN_PASSWORD (Codespaces secret)                │
│                                                                    │
│  Share the forwarded port-3000 URL with your team so they can       │
│  sign in with their own accounts (create them via /users).          │
╰──────────────────────────────────────────────────────────────────╯
BANNER
