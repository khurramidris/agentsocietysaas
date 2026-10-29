#!/usr/bin/env bash
# Run the browser end-to-end suite (services/web/e2e/run.cjs) against a throwaway
# control plane: SQLite, the mock model provider, the local sandbox driver and the
# built web app, all under a temporary directory that is removed afterwards.
#
#   scripts/e2e.sh [step name ...]
#
# Needs the Python environment with controlplane, sandboxd and agentsociety2
# (E2E_PYTHON, default .venv/bin/python), Node 22, Playwright with Chromium, and
# Claude Code on PATH for the AI scientist step (set E2E_SKIP_CHAT=1 without it).
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="${E2E_PYTHON:-$repo/.venv/bin/python}"
port="${E2E_PORT:-8765}"
assets="${E2E_ASSETS:-$repo/.data/assets/extension}"
work="$(mktemp -d)"
base="http://127.0.0.1:$port"

[ -d "$assets" ] || "$repo/scripts/prepare_sandbox_assets.sh" "$assets"
[ -f "$repo/services/web/dist/index.html" ] || (cd "$repo" && npm run build -w services/web)

cleanup() {
  [ -n "${cp_pid:-}" ] && kill "$cp_pid" 2>/dev/null && wait "$cp_pid" 2>/dev/null || true
  # Sandboxes run in their own process groups (they outlive control plane restarts).
  find "$work" -name pid -type f 2>/dev/null | while read -r f; do
    kill -TERM -- "-$(cat "$f")" 2>/dev/null || true
  done
  rm -rf "$work"
}
trap cleanup EXIT

export CP_ENVIRONMENT=development \
  CP_DATABASE_URL="sqlite+aiosqlite:///$work/cp.db" \
  CP_DATA_DIR="$work/data" \
  CP_PORT="$port" \
  CP_PUBLIC_BASE_URL="$base" \
  CP_INTERNAL_BASE_URL="$base" \
  CP_WEB_DIST_DIR="$repo/services/web/dist" \
  CP_ENABLE_MOCK_PROVIDER=true \
  CP_SANDBOX_DRIVER=local \
  CP_LOCAL_SANDBOX_PYTHON="$py" \
  CP_LOCAL_SANDBOX_ASSETS_DIR="$assets" \
  CP_SANDBOX_ENABLE_IDE=false \
  CP_DEFAULT_SIM_MODEL=mock \
  CP_DEFAULT_EMBEDDING_MODEL=mock-embedding \
  CP_DEFAULT_CHAT_MODEL=mock \
  CP_DEFAULT_CHAT_SMALL_MODEL=mock \
  CP_BOOTSTRAP_ADMIN_EMAIL=admin@example.com \
  CP_BOOTSTRAP_ADMIN_PASSWORD=admin-password-123

(cd "$repo/services/control-plane" && exec "$py" -m controlplane) > "$work/controlplane.log" 2>&1 &
cp_pid=$!
for _ in $(seq 1 150); do
  curl -sf "$base/healthz" >/dev/null && break
  kill -0 "$cp_pid" 2>/dev/null || { cat "$work/controlplane.log"; exit 1; }
  sleep 0.2
done

status=0
(cd "$repo/services/web" && E2E_BASE_URL="$base" node e2e/run.cjs "$@") || status=$?
if [ "$status" -ne 0 ]; then
  echo "--- control plane log (last 60 lines)"
  tail -n 60 "$work/controlplane.log"
fi
exit "$status"
