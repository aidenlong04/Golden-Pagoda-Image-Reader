#!/usr/bin/env bash
# Event-driven golden-pagoda health watchdog.
#
# Instead of polling `docker inspect` on a timer, this blocks on the
# `docker events` stream and reacts only when the container reports
# `health_status: unhealthy` or `die`. Idle CPU/RAM are near zero.
#
# Safety: a cooldown between restarts and a sliding-window cap prevent
# thrash if the bot enters a crash loop.

set -euo pipefail

CONTAINER="golden-pagoda"
SERVICE="golden-pagoda.service"

COOLDOWN=60          # min seconds between restarts
MAX_RESTARTS=5       # max restarts within WINDOW_SEC before bailing
WINDOW_SEC=600       # 10 min sliding window

DOCKER=/usr/bin/docker
SYSTEMCTL=/usr/bin/systemctl
LOGGER=/usr/bin/logger

log() { "$LOGGER" -t gp-watchdog -- "$*"; }

last_restart=0
declare -a restart_times=()

prune_window() {
    local now=$1 cutoff=$((now - WINDOW_SEC))
    local kept=() t
    for t in "${restart_times[@]+"${restart_times[@]}"}"; do
        [[ $t -ge $cutoff ]] && kept+=("$t")
    done
    restart_times=("${kept[@]+"${kept[@]}"}")
}

maybe_restart() {
    local reason=$1 now
    now=$(date +%s)

    if (( now - last_restart < COOLDOWN )); then
        log "skip ($reason): cooldown $((COOLDOWN - (now - last_restart)))s left"
        return
    fi

    prune_window "$now"
    if (( ${#restart_times[@]} >= MAX_RESTARTS )); then
        log "ABORT ($reason): $MAX_RESTARTS restarts in last ${WINDOW_SEC}s — needs manual triage"
        return
    fi

    log "restarting $SERVICE ($reason)"
    if "$SYSTEMCTL" restart "$SERVICE"; then
        last_restart=$now
        restart_times+=("$now")
    else
        log "restart failed for $SERVICE"
    fi
}

# Startup sanity check — handles the case where the watchdog was offline
# while the container went bad.
initial="$("$DOCKER" inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
case "$initial" in
    unhealthy|missing|"") maybe_restart "startup=$initial" ;;
esac

# Block on the docker event stream. The pipeline exits if dockerd dies;
# systemd's Restart=always re-execs us.
"$DOCKER" events \
    --filter container="$CONTAINER" \
    --filter event=health_status \
    --filter event=die \
    --format '{{.Action}}' \
| while IFS= read -r ev; do
    case "$ev" in
        "health_status: unhealthy")
            maybe_restart "health=unhealthy"
            ;;
        die)
            # 'die' may fire on graceful stop too; only act if still down.
            sleep 5
            running="$("$DOCKER" inspect --format='{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)"
            [[ "$running" != "true" ]] && maybe_restart "container-died"
            ;;
    esac
done
