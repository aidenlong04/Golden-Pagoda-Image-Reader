#!/usr/bin/env bash
# Restart the golden-pagoda service if its container is unhealthy or missing.
# Designed to run from a systemd timer once a minute.

set -euo pipefail

CONTAINER="golden-pagoda"
SERVICE="golden-pagoda.service"

status="$(/usr/bin/docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")"

case "$status" in
    healthy|starting)
        exit 0
        ;;
    unhealthy|missing|"")
        /usr/bin/logger -t gp-watchdog "container status=$status — restarting $SERVICE"
        /usr/bin/systemctl restart "$SERVICE"
        ;;
    *)
        /usr/bin/logger -t gp-watchdog "unknown container status=$status — leaving alone"
        ;;
esac
