#!/usr/bin/env bash
set -euo pipefail

DEAL_PHONE_SSH_HOST="${DEAL_PHONE_SSH_HOST:-${DEAL_RELAY_SSH_HOST:-}}"
: "${DEAL_PHONE_SSH_HOST:?Set DEAL_PHONE_SSH_HOST or DEAL_RELAY_SSH_HOST}"

DEAL_PHONE_SSH_USER="${DEAL_PHONE_SSH_USER:-${DEAL_RELAY_SSH_USER:-root}}"
DEAL_PHONE_SSH_KEY="${DEAL_PHONE_SSH_KEY:-${DEAL_RELAY_SSH_KEY:-$HOME/.ssh/deal_relay_ed25519}}"
DEAL_PHONE_REMOTE_PORT="${DEAL_PHONE_REMOTE_PORT:-18767}"
DEAL_PHONE_LOCAL_PORT="${DEAL_PHONE_LOCAL_PORT:-8767}"

if [[ ! -r "$DEAL_PHONE_SSH_KEY" ]]; then
  echo "SSH key is not readable: $DEAL_PHONE_SSH_KEY" >&2
  exit 1
fi

exec /usr/bin/ssh \
  -i "$DEAL_PHONE_SSH_KEY" \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -N \
  -R "127.0.0.1:${DEAL_PHONE_REMOTE_PORT}:127.0.0.1:${DEAL_PHONE_LOCAL_PORT}" \
  "${DEAL_PHONE_SSH_USER}@${DEAL_PHONE_SSH_HOST}"
