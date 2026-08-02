#!/usr/bin/env bash
# One-command local start: env file, host dirs, containers, auth bootstrap.
# Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

# .env: create from the example on first run.
if [ ! -f .env ]; then
  cp .env.example .env
  echo "created .env from .env.example"
  # Pre-seed the dev Zitadel URL so the backend can pass its startup guard on
  # first boot (before zitadel-dev-setup.sh runs).  The setup script will
  # overwrite these with the correct values (including CLIENT_ID) once Zitadel
  # is running.
  if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' 's|^ZITADEL_ISSUER=.*|ZITADEL_ISSUER=http://localhost:8300|' .env
    sed -i '' 's|^VITE_ZITADEL_AUTHORITY=.*|VITE_ZITADEL_AUTHORITY=http://localhost:8300|' .env
  else
    sed -i 's|^ZITADEL_ISSUER=.*|ZITADEL_ISSUER=http://localhost:8300|' .env
    sed -i 's|^VITE_ZITADEL_AUTHORITY=.*|VITE_ZITADEL_AUTHORITY=http://localhost:8300|' .env
  fi
  echo "pre-seeded dev Zitadel URL (will be confirmed by zitadel-dev-setup.sh)"
fi

# Host dirs used as bind mounts — create them before Docker does (root-owned).
mkdir -p "$HOME/.opensweep/sandboxes"

echo "→ starting containers (first run builds images; takes a few minutes)…"
docker compose up -d --build

echo "→ waiting for Zitadel to initialize…"
ok=""
for _ in $(seq 1 90); do
  if [ "$(docker inspect -f '{{.State.Health.Status}}' opensweep_zitadel 2>/dev/null)" = "healthy" ] \
     && docker cp opensweep_zitadel:/zitadel/bootstrap/admin.pat /tmp/opensweep-pat-check 2>/dev/null \
     && rm -f /tmp/opensweep-pat-check; then
    ok=1; break
  fi
  sleep 3
done
if [ -z "$ok" ]; then
  echo "✗ Zitadel did not become ready. Check: docker compose logs opensweep_zitadel" >&2
  exit 1
fi

echo "→ configuring auth (Zitadel project, app, users) and writing .env…"
scripts/zitadel-dev-setup.sh

echo
echo "✓ OpenSweep is running:"
echo "    Frontend   http://127.0.0.1:5174"
echo "    Backend    http://127.0.0.1:8001  (/health, /docs)"
echo "    Log in as  qa@opensweep.localhost / OpenSweepQA-Password1!"
echo
echo "  Next steps: add an LLM provider (Admin → LLM Providers), then connect"
echo "  GitHub by pasting a fine-grained token in the welcome wizard (or run"
echo "  scripts/github-app-setup.sh dev for the GitHub App — one browser click)."
