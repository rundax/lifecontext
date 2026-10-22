#!/bin/sh
# Write the strings that qualification evidence must never contain to the file
# named by $1 (mode 0600), and mask them in the job log: every CI model key, the
# gateway URL, its host and any distinctive path segment. Reads
# LIFECONTEXT_CI_LLM_API_KEY, LIFECONTEXT_CI_LLM_BASE_URL and
# LIFECONTEXT_CI_ANTHROPIC_API_KEY from the environment and leaves out any that
# is empty: an empty line in a `grep -F -f` pattern file matches every line.
set -eu

out=$1
url=${LIFECONTEXT_CI_LLM_BASE_URL:-}
rest=${url#*://}
host=${rest%%/*}
host=${host%%:*}
path=""
case "$rest" in */*) path=${rest#*/} ;; esac
# A bare API version such as "v1" is too common to scan for; anything more is not.
path=${path%/}
path=${path%v1}
path=${path%/}
[ "${#path}" -ge 4 ] || path=""

umask 077
: >"$out"
for value in "${LIFECONTEXT_CI_LLM_API_KEY:-}" "${LIFECONTEXT_CI_ANTHROPIC_API_KEY:-}" "$url" "$host" "$path"; do
	if [ -n "$value" ]; then
		printf '%s\n' "$value" >>"$out"
	fi
done
for value in "$host" "$path"; do
	if [ -n "$value" ]; then
		echo "::add-mask::$value"
	fi
done
