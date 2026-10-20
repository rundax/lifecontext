#!/bin/sh
# Regenerate the hashed, fully pinned dependency locks that install.sh and the
# Docker image install with `--require-hashes`, one per supported platform.
# Run from the repository root with the uv that install.sh pins:
#
#   sh requirements/compile.sh            # refresh after changing pyproject.toml
#   sh requirements/compile.sh --check    # fail if a lock no longer matches (CI)
#
# Pins already in a lock are kept (uv reuses them as preferences), so a refresh
# only changes what pyproject.toml or constraints-cpu.txt changed. To move every
# dependency to its newest allowed version, delete the locks first.

set -eu

UV=${UV:-uv}
check=0
[ "${1:-}" != --check ] || check=1

compile() {
	platform=$1 target=$2
	shift 2
	output="requirements/$platform.txt"
	if [ "$check" -eq 1 ]; then
		work=$(mktemp)
		cp "$output" "$work"
	else
		work=$output
	fi
	"$UV" pip compile pyproject.toml --quiet --python-version 3.12 --python-platform "$target" \
		--generate-hashes --emit-index-url --no-build --index-strategy unsafe-best-match \
		--custom-compile-command "sh requirements/compile.sh" --output-file "$work" "$@"
	if [ "$check" -eq 1 ]; then
		if ! diff -u "$output" "$work"; then
			echo "requirements/$platform.txt is stale: run sh requirements/compile.sh and commit the result" >&2
			rm -f "$work"
			exit 1
		fi
		rm -f "$work"
		echo "requirements/$platform.txt is current"
	fi
}

# CPU-only PyTorch on Linux; pg0-embedded publishes manylinux_2_35 and macOS 14 wheels.
compile linux-x86_64 x86_64-manylinux_2_35 \
	--constraints constraints-cpu.txt --extra-index-url https://download.pytorch.org/whl/cpu
compile linux-aarch64 aarch64-manylinux_2_35 \
	--constraints constraints-cpu.txt --extra-index-url https://download.pytorch.org/whl/cpu
export MACOSX_DEPLOYMENT_TARGET=14.0
compile macos-arm64 aarch64-apple-darwin
