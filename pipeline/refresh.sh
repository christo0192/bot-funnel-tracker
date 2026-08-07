#!/usr/bin/env bash
# refresh.sh - daily rebuild + deploy of the IK Bot Console dashboard (WSL side).
# Runs entirely in WSL, where the Vercel CLI is already logged in as christo.b and
# python3 + google-cloud-bigquery + the service-account key are all available.
# Triggered daily by a Windows Scheduled Task via wsl.exe (see register-wsl-task).
set -uo pipefail

ROOT="/mnt/c/Users/Admin/Claude projects/Bot calling tracker"
KEY="${GOOGLE_APPLICATION_CREDENTIALS:-/mnt/c/Users/Admin/.gcp-keys/claude-code-dev-bq.json}"
LOG="$ROOT/pipeline/last-refresh.log"
export GOOGLE_APPLICATION_CREDENTIALS="$KEY"
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

cd "$ROOT" || { echo "cannot cd to project"; exit 1; }
{
  echo "[`date -Is`] refresh start"
  echo "[`date -Is`] build_data"
  python3 pipeline/build_data.py --out data.json || { echo "build_data FAILED"; exit 1; }
  echo "[`date -Is`] inject"
  python3 pipeline/inject.py || { echo "inject FAILED"; exit 1; }
  echo "[`date -Is`] deploy"
  vercel deploy --prod --yes --cwd site || { echo "deploy FAILED"; exit 1; }
  echo "[`date -Is`] refresh done"
} 2>&1 | tee "$LOG"
