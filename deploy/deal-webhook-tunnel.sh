#!/usr/bin/env bash
set -euo pipefail

: "${DEAL_WEBHOOK_SSH_HOST:?DEAL_WEBHOOK_SSH_HOST is required}"

DEAL_WEBHOOK_SSH_USER="${DEAL_WEBHOOK_SSH_USER:-root}"
DEAL_WEBHOOK_SSH_KEY="${DEAL_WEBHOOK_SSH_KEY:-$HOME/.ssh/deal_relay_ed25519}"
DEAL_WEBHOOK_REMOTE_PORT="${DEAL_WEBHOOK_REMOTE_PORT:-18765}"
DEAL_WEBHOOK_LOCAL_PORT="${DEAL_WEBHOOK_LOCAL_PORT:-8765}"

if [[ ! -r "$DEAL_WEBHOOK_SSH_KEY" ]]; then
  echo "SSH key is not readable: $DEAL_WEBHOOK_SSH_KEY" >&2
  exit 1
fi

# Keep the remotely exposed socket loopback-only on the VPS. Caddy/nginx on the
# VPS is the only public entry point; the main watcher host remains unreachable
# from the Internet even when it sits behind CGNAT.
exec /usr/bin/ssh \
  -i "$DEAL_WEBHOOK_SSH_KEY" \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -N \
  -R "127.0.0.1:${DEAL_WEBHOOK_REMOTE_PORT}:127.0.0.1:${DEAL_WEBHOOK_LOCAL_PORT}" \
  "${DEAL_WEBHOOK_SSH_USER}@${DEAL_WEBHOOK_SSH_HOST}"
