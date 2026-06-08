#!/usr/bin/env bash
# Golden Pagoda ops helper — small functions for status / restart / deploy / health.
#
# Source this file:    source scripts/ops.sh
# Then call:           gp-status  |  gp-restart  |  gp-deploy  |  gp-health  |  gp-logs
#
# Or invoke directly:  scripts/ops.sh <status|restart|deploy|health|logs> [args...]
#
# Connects to the Hetzner host over SSH using $GP_HOST and $GP_KEY (with sane defaults).

GP_HOST="${GP_HOST:-nomekui@5.78.211.130}"
GP_KEY="${GP_KEY:-$HOME/.ssh/hetzner}"
GP_SERVICE="${GP_SERVICE:-golden-pagoda}"
GP_CONTAINER="${GP_CONTAINER:-golden-pagoda}"

_gp_ssh() {
    ssh -i "$GP_KEY" -o StrictHostKeyChecking=accept-new "$GP_HOST" "$@"
}

gp-status() {
    echo "── systemd ──────────────────────────────────────"
    _gp_ssh "systemctl is-active $GP_SERVICE; systemctl is-enabled $GP_SERVICE; sudo systemctl status $GP_SERVICE --no-pager -n 5 || true"
    echo
    echo "── docker ───────────────────────────────────────"
    _gp_ssh "sudo docker ps --filter name=^/${GP_CONTAINER}\$ --format 'table {{.Names}}\t{{.Status}}\t{{.RunningFor}}\t{{.Image}}'"
}

gp-restart() {
    echo ">> restarting $GP_SERVICE on $GP_HOST"
    _gp_ssh "sudo systemctl restart $GP_SERVICE"
    sleep 3
    gp-status
}

gp-deploy() {
    local repo_root
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
    echo ">> manual deploy from $repo_root → $GP_HOST"
    "$repo_root/scripts/deploy_hetzner.sh" "$GP_HOST" "$GP_KEY"
}

gp-health() {
    echo "── container inspect ────────────────────────────"
    _gp_ssh "sudo docker inspect --format='State={{.State.Status}} Started={{.State.StartedAt}} Restarts={{.RestartCount}} ExitCode={{.State.ExitCode}}' $GP_CONTAINER 2>/dev/null || echo 'container not found'"
    echo
    echo "── recent errors (last 200 log lines) ───────────"
    _gp_ssh "sudo docker logs --tail 200 $GP_CONTAINER 2>&1 | grep -iE 'error|traceback|exception|fatal|critical' | tail -10 || echo '(none)'"
    echo
    echo "── disk / memory ────────────────────────────────"
    _gp_ssh "df -h /opt /var/lib/docker | tail -2; free -h | head -2"
}

gp-logs() {
    local n="${1:-50}"
    _gp_ssh "sudo docker logs --tail $n -f $GP_CONTAINER"
}

# Update a single key in the server's /opt/golden-pagoda/.env and restart.
# Usage:  gp-env-set KEY "VALUE"
# Notes:
#   - The server's .env is the source of truth at runtime. The repo's .env
#     is excluded from the deploy rsync, so commits do NOT touch it.
#   - Replaces an existing KEY=... line in place, or appends if missing.
#   - VALUE is passed verbatim; quote it if it contains spaces, '<', '>',
#     or shell metacharacters (e.g. emoji literals like '<:Foo:123>').
#   - Restarts golden-pagoda.service so the new value is picked up.
gp-env-set() {
    local key="$1"
    local value="$2"
    if [[ -z "$key" ]]; then
        echo "usage: gp-env-set KEY VALUE" >&2
        return 2
    fi
    # Guard against regex/awk metacharacters corrupting the .env rewrite.
    if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "gp-env-set: invalid key '$key' (use A-Z, 0-9, _)" >&2
        return 2
    fi
    # Base64 the value to avoid any quoting / sed-delimiter pain over SSH.
    local b64
    b64="$(printf '%s' "$value" | base64 -w0)"
    _gp_ssh "
        set -e
        ENV_FILE=/opt/golden-pagoda/.env
        VAL=\$(printf '%s' '$b64' | base64 -d)
        if sudo grep -qE '^$key=' \"\$ENV_FILE\"; then
            # Use a delimiter unlikely to appear in env values.
            sudo awk -v k='$key' -v v=\"\$VAL\" 'BEGIN{FS=OFS=\"=\"} \$1==k {print k\"=\"v; next} {print}' \"\$ENV_FILE\" | sudo tee \"\$ENV_FILE.tmp\" > /dev/null
            sudo mv \"\$ENV_FILE.tmp\" \"\$ENV_FILE\"
        else
            echo \"$key=\$VAL\" | sudo tee -a \"\$ENV_FILE\" > /dev/null
        fi
        # Keep the secrets file owned by the container uid + non-readable to
        # others (the tee/mv above can reset it to root:root 0644).
        sudo chown 10001:10001 \"\$ENV_FILE\" 2>/dev/null || true
        sudo chmod 0600 \"\$ENV_FILE\"
        # Confirm the key is present WITHOUT echoing its (possibly secret) value.
        if sudo grep -qE '^$key=' \"\$ENV_FILE\"; then echo '$key updated'; else echo '$key MISSING after write' >&2; fi
        sudo systemctl restart $GP_SERVICE
        sleep 3
        systemctl is-active $GP_SERVICE
    "
}

# Print one or more keys from the server's .env. Usage: gp-env-get KEY [KEY...]
# Secret-looking keys (token/key/secret/password) are masked so they don't
# leak into the terminal / CI logs; everything else is printed verbatim.
gp-env-get() {
    local keys="$*"
    if [[ -z "$keys" ]]; then
        echo "usage: gp-env-get KEY [KEY...]" >&2
        return 2
    fi
    local pattern
    pattern="^($(echo "$keys" | tr ' ' '|'))="
    _gp_ssh "sudo grep -nE '$pattern' /opt/golden-pagoda/.env \
        | sed -E 's/^([0-9]+:[A-Za-z0-9_]*(TOKEN|KEY|SECRET|PASSWORD)[A-Za-z0-9_]*=).*/\1****(masked)/I' \
        || echo '(no matches)'"
}

# Allow direct invocation:  scripts/ops.sh <cmd> [args...]
if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
    cmd="${1:-status}"
    shift || true
    case "$cmd" in
        status)  gp-status ;;
        restart) gp-restart ;;
        deploy)  gp-deploy ;;
        health)  gp-health ;;
        logs)    gp-logs "$@" ;;
        env-set) gp-env-set "$@" ;;
        env-get) gp-env-get "$@" ;;
        *) echo "usage: $0 {status|restart|deploy|health|logs [N]|env-set KEY VALUE|env-get KEY [KEY...]}" >&2; exit 2 ;;
    esac
fi
