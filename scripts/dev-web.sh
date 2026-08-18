#!/usr/bin/env bash
# Local dev board against the live cluster's API.
#
# Starts a read-only port-forward to the api Service and runs the Astro dev
# server on top of it (astro.config.mjs proxies /api to localhost:8000), so
# UI changes hot-reload against real data without touching production.
# Ctrl-C stops both.
set -euo pipefail

cd "$(dirname "$0")/.."

kubectl -n blockade port-forward svc/api 8000:80 >/dev/null &
FORWARD_PID=$!
trap 'kill "$FORWARD_PID" 2>/dev/null' EXIT

# The proxy 404s if the dev server starts before the forward is ready.
for _ in $(seq 1 20); do
  curl -sf -o /dev/null http://localhost:8000/healthz && break
  sleep 0.5
done

cd services/api/web
npm run dev
