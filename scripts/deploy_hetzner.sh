#!/usr/bin/env bash
# Deploy the bot to a Hetzner (or any Ubuntu) server over SSH.
# Usage:  ./scripts/deploy_hetzner.sh root@<server-ip> [ssh-key-path]
#
# Requires the server to already have Docker installed (the script will
# install it if missing). Idempotent: safe to re-run after pulling new code.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 user@host [ssh-key-path]" >&2
    exit 2
fi

REMOTE="$1"
KEY="${2:-$HOME/.ssh/hetzner}"
APP_DIR="/opt/golden-pagoda"
SERVICE="golden-pagoda"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new)
RSYNC_RSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"

echo ">> ensuring Docker is installed on $REMOTE"
"${SSH[@]}" "$REMOTE" 'set -e
if ! command -v docker >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg rsync
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
    systemctl enable --now docker
fi
mkdir -p '"$APP_DIR"'
'

echo ">> syncing repo to $REMOTE:$APP_DIR"
rsync -az --delete -e "$RSYNC_RSH" \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.venv' \
    "$REPO_ROOT/" "$REMOTE:$APP_DIR/"

echo ">> installing systemd unit"
"${SSH[@]}" "$REMOTE" "install -m 0644 $APP_DIR/scripts/$SERVICE.service /etc/systemd/system/$SERVICE.service && systemctl daemon-reload"

echo ">> building image"
"${SSH[@]}" "$REMOTE" "cd $APP_DIR && docker build -t golden-pagoda:latest . | tail -3"

echo ">> (re)starting service"
"${SSH[@]}" "$REMOTE" "systemctl enable --now $SERVICE && systemctl restart $SERVICE && sleep 5 && systemctl is-active $SERVICE"

echo ">> recent logs:"
"${SSH[@]}" "$REMOTE" "docker logs $SERVICE --tail 20 2>&1 || true"

echo "done."
