#!/usr/bin/env bash
# MasG EV Tool -- OFFLINE DEMO MODE launcher (macOS / Linux / Git Bash).
#
# Runs the backend + dashboard on synthetic example data:
#   * no API keys needed (backend/.env is not required; no keys are used)
#   * no requests to Kalshi, Polymarket or The Odds API
#   * no Pushover notifications (monitor start/test are refused with 403)
#   * no orders of any kind (the tool never places orders; the paper maker
#     cannot be enabled in demo mode)
#
# The synthetic markets still go through the REAL pipeline (adapters ->
# NormalizedMarket -> Pass A features -> Pass B rule engine -> Kelly), so
# every BUY / WATCH / SKIP shown is produced by the rule engine.
# Opening the dashboard (or clicking Scan) populates the demo snapshot.
#
# Prerequisites: backend deps installed (pip install -r backend/requirements.txt,
# optionally into backend/.venv) and frontend deps (cd frontend && npm install).
# No Node? Run the pipeline once in the terminal instead:
#     cd backend && MASG_DEMO_MODE=1 python -m services.demo --all
#
# Windows (cmd.exe) equivalent -- two terminals:
#     cd backend && set "MASG_DEMO_MODE=1" && .venv\Scripts\activate && uvicorn main:app --host 127.0.0.1 --port 8000
#     cd frontend && set "NEXT_PUBLIC_BACKEND_URL=http://127.0.0.1:8000" && npm run dev
# Windows (PowerShell):
#     cd backend; $env:MASG_DEMO_MODE="1"; .venv\Scripts\Activate.ps1; uvicorn main:app --host 127.0.0.1 --port 8000
#     cd frontend; $env:NEXT_PUBLIC_BACKEND_URL="http://127.0.0.1:8000"; npm run dev
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MASG_DEMO_MODE=1
# Point the dashboard at the IPv4 loopback the backend binds to.
export NEXT_PUBLIC_BACKEND_URL="${NEXT_PUBLIC_BACKEND_URL:-http://127.0.0.1:8000}"
export NEXT_TELEMETRY_DISABLED=1

if [[ -x "$ROOT/backend/.venv/bin/python" ]]; then
  PY="$ROOT/backend/.venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  echo "frontend/node_modules not found -- run: (cd frontend && npm install)" >&2
  exit 1
fi

pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

echo "Starting backend (DEMO MODE) on http://127.0.0.1:8000 ..."
(cd "$ROOT/backend" && exec "$PY" -m uvicorn main:app --host 127.0.0.1 --port 8000) &
pids+=($!)

echo "Starting dashboard on http://localhost:3000 ..."
(cd "$ROOT/frontend" && exec npm run dev) &
pids+=($!)

echo
echo "Demo mode: synthetic example data -- open http://localhost:3000"
echo "Press Ctrl+C to stop both."
wait
