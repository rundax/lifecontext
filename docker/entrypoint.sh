#!/bin/sh
# Entrypoint of the LifeContext image.
#
#   (no arguments) or "serve": set up ~/.lifeweave on the first start from the
#     environment and the key file, then run `lifecontext serve` on loopback; on
#     docker stop, stop it and the Hindsight memory engine with its Postgres
#   anything else: run that lifecontext command instead, for example
#     docker compose run --rm lifecontext backup
#
# Environment: LIFECONTEXT_PROVIDER, LIFECONTEXT_MODEL, LIFECONTEXT_BASE_URL,
# LIFECONTEXT_TIMEZONE (first start only); the key comes from
# LIFECONTEXT_API_KEY_FILE (default /run/secrets/lifecontext_api_key, one line)
# or, less privately, LIFECONTEXT_API_KEY, and is stored on every start. The
# vault is always /data/vault.

set -eu
umask 077

if [ "$#" -gt 0 ] && [ "$1" != serve ]; then
	exec lifecontext "$@"
fi

port=8765
lifeweave="$HOME/.lifeweave"

log() { printf 'lifecontext-entrypoint: %s\n' "$*" >&2; }

# init itself reads LIFECONTEXT_PROVIDER, LIFECONTEXT_MODEL, LIFECONTEXT_BASE_URL
# and the key file named by LIFECONTEXT_API_KEY_FILE; an empty or missing secret
# file means no key.
key_file=${LIFECONTEXT_API_KEY_FILE:-/run/secrets/lifecontext_api_key}
unset LIFECONTEXT_API_KEY_FILE
if [ -f "$key_file" ] && [ -s "$key_file" ]; then
	if [ ! -r "$key_file" ]; then
		log "cannot read the key file $key_file as uid $(id -u); make it readable by that uid (for example chown $(id -u) on the host file, mode 0600)"
		exit 1
	fi
	export LIFECONTEXT_API_KEY_FILE="$key_file"
fi

run_init() {
	if [ -z "${LIFECONTEXT_API_KEY_FILE:-}" ] && [ -n "${LIFECONTEXT_API_KEY:-}" ]; then
		printf '%s\n' "$LIFECONTEXT_API_KEY" | lifecontext init "$@" --api-key-stdin
	else
		lifecontext init "$@" </dev/null
	fi
}

# Print init's JSON report without the endpoint URL; fail on {"error": ...}.
report_init() {
	python -I -c '
import json, sys
report = json.loads(sys.stdin.read() or "{}")
if "error" in report or "credentials_state" not in report:
    print("lifecontext-entrypoint: lifecontext init failed: %s" % report.get("error", "no report"), file=sys.stderr)
    sys.exit(1)
print("lifecontext-entrypoint: %s %s, config %s, key %s, vault %s" % (
    report["provider"], report["model"], report["config_state"], report["credentials_state"],
    report["vault_root"]), file=sys.stderr)
'
}

if [ ! -f "$lifeweave/config.yaml" ]; then
	if [ -z "${LIFECONTEXT_PROVIDER:-}${LIFECONTEXT_MODEL:-}${LIFECONTEXT_API_KEY_FILE:-}${LIFECONTEXT_API_KEY:-}" ]; then
		log "not set up yet: set LIFECONTEXT_MODEL (and LIFECONTEXT_PROVIDER, LIFECONTEXT_BASE_URL) and the key file, or run"
		log "  docker exec -it CONTAINER lifecontext init --vault /data/vault"
		log "waiting for ~/.lifeweave/config.yaml"
		until [ -f "$lifeweave/config.yaml" ]; do
			sleep 5
		done
	else
		set -- --non-interactive --json --vault /data/vault
		[ -z "${LIFECONTEXT_TIMEZONE:-}" ] || set -- "$@" --timezone "$LIFECONTEXT_TIMEZONE"
		report=$(run_init "$@") || true
		printf '%s' "$report" | report_init
	fi
elif [ -n "${LIFECONTEXT_API_KEY_FILE:-}${LIFECONTEXT_API_KEY:-}" ]; then
	# The secret is the key: store it on every start, so a restore (backups never
	# hold the key) or a changed secret takes effect.
	report=$(run_init --non-interactive --replace-key --json) || true
	printf '%s' "$report" | report_init
fi
unset LIFECONTEXT_API_KEY

# The tokenizer file Hindsight would otherwise download on the first start.
if [ -d /opt/lifecontext/tiktoken ] && [ -d "$lifeweave" ]; then
	mkdir -p "$lifeweave/cache/tiktoken"
	chmod 700 "$lifeweave/cache" "$lifeweave/cache/tiktoken"
	for seeded in /opt/lifecontext/tiktoken/*; do
		[ -f "$seeded" ] || continue
		[ -e "$lifeweave/cache/tiktoken/${seeded##*/}" ] || cp "$seeded" "$lifeweave/cache/tiktoken/"
	done
fi

stop_engine() {
	if ! result=$(lifecontext hindsight stop --json 2>/dev/null); then
		log "the memory engine did not stop cleanly: $result"
	fi
}

# shellcheck disable=SC2317 # called from the trap
terminate() {
	trap - TERM INT
	kill -TERM "$serve_pid" 2>/dev/null || true
	wait "$serve_pid" 2>/dev/null || true
	stop_engine
	exit 0
}

lifecontext serve --port "$port" &
serve_pid=$!
trap terminate TERM INT
status=0
wait "$serve_pid" || status=$?
trap - TERM INT
# serve stopped by itself: stop the engine too, so the next start begins clean.
stop_engine
exit "$status"
