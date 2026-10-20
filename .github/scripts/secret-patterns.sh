#!/bin/sh
# Write the strings that qualification evidence must never contain to the file
# named by $1 (mode 0600), and mask them in the job log: the CI model key, the
# endpoint URL, its host and any distinctive path segment. Reads LLM_API_KEY and
# LLM_BASE_URL from the environment.
set -eu

out=$1
url=${LLM_BASE_URL#*://}
host=${url%%/*}
host=${host%%:*}
path=""
case "$url" in */*) path=${url#*/} ;; esac
# A bare API version such as "v1" is too common to scan for; anything more is not.
path=${path%/}
path=${path%v1}
path=${path%/}

umask 077
{
	printf '%s\n' "$LLM_API_KEY" "$LLM_BASE_URL" "$host"
	if [ "${#path}" -ge 4 ]; then
		printf '%s\n' "$path"
	fi
} >"$out"
echo "::add-mask::$host"
if [ "${#path}" -ge 4 ]; then
	echo "::add-mask::$path"
fi
