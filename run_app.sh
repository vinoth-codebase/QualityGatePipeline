#!/usr/bin/env bash
# ------------------------------------------------------------------
# One-command launcher for macOS (no Docker).
#   ./run_app.sh            -> set up venv (if needed), seed data, launch Streamlit
#   ./run_app.sh --reset    -> wipe & re-seed demo data first
# ------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ ! -d ".venv" ]; then
  echo "🐍 Creating virtual environment (.venv)..."
  "$PYTHON_BIN" -m venv .venv
  ./.venv/bin/pip install --upgrade pip >/dev/null
  ./.venv/bin/pip install -r requirements.txt
fi

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo "📝 Created .env from .env.example (SMTP in local fallback mode until configured)."
fi

if [ "${1:-}" = "--reset" ]; then
  ./.venv/bin/python -m src.data_generator --force
else
  ./.venv/bin/python -m src.data_generator
fi

echo "🚀 Launching Ops Quality Gatekeeper console at http://localhost:8501"
exec ./.venv/bin/streamlit run app.py --server.headless true --browser.gatherUsageStats false
