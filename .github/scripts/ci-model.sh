#!/bin/sh
# Pick the CI model key for LLM_PROVIDER and stop when it, the gateway URL it
# needs or LLM_MODEL is missing. Writes, in the directory named by $1 (mode
# 0700): llm-api-key, that one key, and secret-patterns (secret-patterns.sh),
# which lists every CI key and the gateway endpoint whatever the provider.
# openai_compatible goes through the gateway at LIFECONTEXT_CI_LLM_BASE_URL;
# anthropic goes to the provider's own API, so the jobs set no base URL for it.
set -eu

dir=$1
missing=""
case "${LLM_PROVIDER:-}" in
openai_compatible)
	key=${LIFECONTEXT_CI_LLM_API_KEY:-}
	[ -n "$key" ] || missing="$missing secret LIFECONTEXT_CI_LLM_API_KEY;"
	[ -n "${LIFECONTEXT_CI_LLM_BASE_URL:-}" ] || missing="$missing secret LIFECONTEXT_CI_LLM_BASE_URL;"
	[ -n "${LLM_MODEL:-}" ] || missing="$missing input model or variable LIFECONTEXT_CI_LLM_MODEL;"
	;;
anthropic)
	key=${LIFECONTEXT_CI_ANTHROPIC_API_KEY:-}
	[ -n "$key" ] || missing="$missing secret LIFECONTEXT_CI_ANTHROPIC_API_KEY;"
	[ -n "${LLM_MODEL:-}" ] || missing="$missing input model;"
	;;
*)
	echo "::error::no CI credentials for provider '${LLM_PROVIDER:-}'"
	exit 1
	;;
esac
if [ -n "$missing" ]; then
	echo "::error::provider $LLM_PROVIDER needs:${missing%;}"
	exit 1
fi

umask 077
mkdir -p "$dir"
sh "$(dirname "$0")/secret-patterns.sh" "$dir/secret-patterns"
printf '%s\n' "$key" >"$dir/llm-api-key"
