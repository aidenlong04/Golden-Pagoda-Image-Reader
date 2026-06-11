#!/usr/bin/env bash
# Deploy the bot to a Hetzner (or any Ubuntu) server over SSH.
# Usage:  ./scripts/deploy_hetzner.sh nomekui@<server-ip> [ssh-key-path]
#
# The remote user must be in the 'docker' and 'sudo' groups (NOPASSWD).
# Idempotent: safe to re-run after pulling new code.

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

echo ">> syncing repo to $REMOTE:$APP_DIR"
"${SSH[@]}" "$REMOTE" "sudo mkdir -p $APP_DIR && sudo chown \$USER:\$USER $APP_DIR"
rsync -az --delete -e "$RSYNC_RSH" \
    --exclude '.git' \
    --exclude '.github' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.venv' \
    --exclude '.env' \
    --exclude 'data' \
    --exclude 'icons' \
    "$REPO_ROOT/" "$REMOTE:$APP_DIR/"

echo ">> installing systemd unit"
"${SSH[@]}" "$REMOTE" "sudo install -m 0644 $APP_DIR/scripts/$SERVICE.service /etc/systemd/system/$SERVICE.service && sudo systemctl daemon-reload"

echo ">> installing watchdog (event-driven service)"
# The old timer-based watchdog was replaced by a long-running
# `docker events`-driven service — disable any stale timer before installing.
"${SSH[@]}" "$REMOTE" "sudo chmod +x $APP_DIR/scripts/golden-pagoda-watchdog.sh \
    && sudo install -m 0644 $APP_DIR/scripts/golden-pagoda-watchdog.service /etc/systemd/system/golden-pagoda-watchdog.service \
    && (sudo systemctl disable --now golden-pagoda-watchdog.timer 2>/dev/null || true) \
    && sudo rm -f /etc/systemd/system/golden-pagoda-watchdog.timer \
    && sudo systemctl daemon-reload \
    && sudo systemctl enable --now golden-pagoda-watchdog.service \
    && sudo systemctl restart golden-pagoda-watchdog.service"

echo ">> building image"
# No `| tail` here: a failed build must propagate its exit code through SSH
# (set -euo pipefail) so we never restart the service on a stale image.
# BuildKit (parity with the GitHub Actions deploy) gives faster, cache-aware
# builds; it's the default on modern Docker but set explicitly for older hosts.
"${SSH[@]}" "$REMOTE" "cd $APP_DIR && DOCKER_BUILDKIT=1 docker build -t golden-pagoda:latest ."

echo ">> (re)starting service"
"${SSH[@]}" "$REMOTE" "sudo systemctl enable --now $SERVICE && sudo systemctl restart $SERVICE && sleep 5 && systemctl is-active $SERVICE"

echo ">> reclaiming disk (keep warm cache)"
# Mirror the workflow: drop only dangling images (keeps the python:3.12-slim
# base for the next build) and cap the build cache rather than wiping it, so
# the next deploy still hits the apt/pip layers instead of a cold build.
"${SSH[@]}" "$REMOTE" "docker image prune -f >/dev/null 2>&1 || true; docker builder prune -f --keep-storage 2GB >/dev/null 2>&1 || true"

echo ">> recent logs:"
"${SSH[@]}" "$REMOTE" "docker logs $SERVICE --tail 20 2>&1 || true"

echo "done."
