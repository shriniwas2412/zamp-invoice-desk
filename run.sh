#!/usr/bin/env bash
# Start Invoice Desk on http://localhost:8077
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
exec .venv/bin/uvicorn app.main:app --port "${PORT:-8077}"
