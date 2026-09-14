#!/usr/bin/env bash
set -euo pipefail

: "${DEAL_RELAY_SSH_HOST:?DEAL_RELAY_SSH_HOST is required}"

DEAL_RELAY_SSH_USER="${DEAL_RELAY_SSH_USER:-root}"
DEAL_RELAY_SSH_KEY="${DEAL_RELAY_SSH_KEY:-$HOME/.ssh/deal_relay_ed25519}"
DEAL_RELAY_LOCAL_PORT="${DEAL_RELAY_LOCAL_PORT:-18770}"
DEAL_RELAY_REMOTE_PORT="${DEAL_RELAY_REMOTE_PORT:-8770}"

if [[ ! -r "$DEAL_RELAY_SSH_KEY" ]]; then
  echo "SSH key is not readable: $DEAL_RELAY_SSH_KEY" >&2
  exit 1
fi

exec /usr/bin/ssh \
  -i "$DEAL_RELAY_SSH_KEY" \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -N \
  -L "127.0.0.1:${DEAL_RELAY_LOCAL_PORT}:127.0.0.1:${DEAL_RELAY_REMOTE_PORT}" \
  "${DEAL_RELAY_SSH_USER}@${DEAL_RELAY_SSH_HOST}"
