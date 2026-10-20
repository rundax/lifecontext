#!/bin/sh
# Superseded by `lifecontext skills`; kept so existing habits keep working. Links the
# packaged journal skill into every agent's skills directory in the real HOME, using this
# checkout's .dev/venv when it exists.
#
#   install_agent_skill.sh          lifecontext skills install --agent all
#   install_agent_skill.sh --check  lifecontext skills check --agent all (exit 1 on drift)
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cli="$repo/.dev/venv/bin/lifecontext"
[ -x "$cli" ] || cli=lifecontext
if [ "${1:-}" = "--check" ]; then
    exec "$cli" skills check --agent all
fi
exec "$cli" skills install --agent all
