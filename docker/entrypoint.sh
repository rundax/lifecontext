#!/bin/sh
# Entrypoint of the LifeContext image.
#
#   (no arguments) or "serve": set up ~/.lifeweave on the first start from the
#     environment and the key file, then run `lifecontext serve` on loopback; on
#     docker stop, stop it and the Hindsight memory engine with its Postgres
#   anything else: run that lifecontext command instead, for example
#     docker compose run --rm lifecontext backup
#
# Environment: LIFECONTEXT_PROVIDER and LIFECONTEXT_MODEL (both required on the
# first start; without them the container waits), LIFECONTEXT_BASE_URL,
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
    print("lifecontext-entrypoint: lifecontext init failed: %s"
          % (report.get("message") or report.get("error", "no report")), file=sys.stderr)
    sys.exit(1)
print("lifecontext-entrypoint: %s %s, config %s, key %s, vault %s" % (
    report["provider"], report["model"], report["config_state"], report["credentials_state"],
    report["vault_root"]), file=sys.stderr)
'
}

# Whether a key is stored, or the endpoint needs none.
key_stored() {
	lifecontext llm show --json 2>/dev/null | python -I -c '
import json, sys
try:
    sys.exit(json.loads(sys.stdin.read()).get("api_key") not in ("set", "not_required"))
except (ValueError, AttributeError):
    sys.exit(1)
'
}

# There is no default model. Wait for the setup rather than exit, which
# restart: unless-stopped would turn into a restart loop: for config.yaml, and
# then, without a key secret, for `lifecontext init` in the container to store the key.
wait_for_setup() {
	log "set LIFECONTEXT_PROVIDER and LIFECONTEXT_MODEL (and LIFECONTEXT_BASE_URL for openai_compatible)"
	log "and the key file, then recreate the container; or run"
	log "  docker exec -it CONTAINER lifecontext init --vault /data/vault"
	log "waiting for ~/.lifeweave/config.yaml"
	trap 'exit 0' TERM INT
	waiting_for_key=0
	until [ -f "$lifeweave/config.yaml" ] &&
		{ [ -n "${LIFECONTEXT_API_KEY_FILE:-}${LIFECONTEXT_API_KEY:-}" ] || key_stored; }; do
		if [ -f "$lifeweave/config.yaml" ] && [ "$waiting_for_key" -eq 0 ]; then
			log "waiting for the model key (lifecontext init asks for it; or lifecontext llm set --api-key-stdin)"
			waiting_for_key=1
		fi
		sleep 5
	done
	trap - TERM INT
}

missing=""
[ -n "${LIFECONTEXT_PROVIDER:-}" ] || missing="LIFECONTEXT_PROVIDER"
[ -n "${LIFECONTEXT_MODEL:-}" ] || missing="${missing:+$missing and }LIFECONTEXT_MODEL"

key_stored_by_init=0
if [ ! -f "$lifeweave/config.yaml" ]; then
	if [ -n "$missing" ]; then
		case "$missing" in
		*" and "*) log "not set up yet: $missing are not set" ;;
		*) log "not set up yet: $missing is not set" ;;
		esac
		wait_for_setup
	else
		set -- --non-interactive --json --vault /data/vault
		[ -z "${LIFECONTEXT_TIMEZONE:-}" ] || set -- "$@" --timezone "$LIFECONTEXT_TIMEZONE"
		report=$(run_init "$@") || true
		if printf '%s' "$report" | report_init; then
			key_stored_by_init=1
		else
			wait_for_setup
		fi
	fi
fi
if [ "$key_stored_by_init" -eq 0 ] && [ -n "${LIFECONTEXT_API_KEY_FILE:-}${LIFECONTEXT_API_KEY:-}" ]; then
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
