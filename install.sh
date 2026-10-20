#!/bin/sh
# Install LifeContext for the current user, or remove what this script installed.
#
#   curl -fsSL https://github.com/Hantok/lifecontext/releases/latest/download/install.sh | sh
#   sh install.sh --help
#
# The installation lives in ~/.local/share/lifecontext (a pinned uv, the Python
# it provides and the virtual environment) plus the link ~/.local/bin/lifecontext.
# Settings and the key (~/.lifeweave), the vault, backups and indexes are never
# removed. LifeContext is source-available under the Business Source License 1.1.

set -eu

LIFECONTEXT_RELEASE="1.2.0"
REPOSITORY="Hantok/lifecontext"
UV_VERSION="0.12.24"
UV_SHA256_LINUX_X86_64="48170bd200a5430298c18f3b264485a1f3a8605f01088277daf6e37633edf0f5"
UV_SHA256_LINUX_AARCH64="7f9ab4726d743b6a92e310477a7840c8a8a7b3938cb3c4657d541342fca75011"
UV_SHA256_MACOS_ARM64="0c4346de7abdb49495b393b9ec809fe387aa43e586be20fecb972216c1e71732"
# The embedding and rerank models Hindsight loads, at fixed revisions (keep in
# step with the Dockerfile; tests/test_install_script.py checks it).
EMBEDDING_MODEL="BAAI/bge-small-en-v1.5"
EMBEDDING_REVISION="5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
RERANKER_MODEL="nreimers/mmarco-mMiniLMv2-L6-H384-v1"
RERANKER_REVISION="4ceabf2d1e212e16da0d1fb94d5dea66a9a1cca0"
API_PORT=8765
HEALTH_WAIT_SECONDS=900
LAUNCHD_LABEL="io.github.hantok.lifecontext"

usage() {
	cat <<'EOF'
Usage: install.sh [options]

Installs LifeContext for the current user: a pinned uv and Python 3.12, the
package and its hash-locked dependencies in ~/.local/share/lifecontext/venv
(CPU-only PyTorch on Linux), the local models at fixed revisions, the command
~/.local/bin/lifecontext and Tesseract OCR with English, Russian and
Ukrainian, then runs `lifecontext init`, `service install --maintenance`,
`skills install` and `doctor`. Running it again upgrades in place: it stops
the service, backs up your data, installs, and starts the service again.

Options:
  --version VERSION     release to install (default: 1.2.0)
  --assets DIR          use release assets from DIR instead of downloading them
                        (SHA256SUMS, the wheel, requirements-PLATFORM.txt and,
                        to check the signature, SHA256SUMS.sigstore.json)
  --wheel PATH          install a locally built wheel ...
  --lock PATH           ... with this lock (requirements/PLATFORM.txt)
  --non-interactive     never prompt; set up from LIFECONTEXT_* variables only
  --no-service          do not install the systemd user service or launchd agent
  --no-skills           do not link the agent skill into detected agents
  --no-backup           do not back up before upgrading an existing installation
  --allow-root          allow running as root (not recommended)
  --uninstall           remove only what this script installed
  -h, --help            show this help

Unattended setup, used instead of prompts when PROVIDER, MODEL or
API_KEY_FILE is set (without a terminal and without them, init is skipped):
  LIFECONTEXT_PROVIDER      provider ID (default: anthropic)
  LIFECONTEXT_MODEL         exact model ID
  LIFECONTEXT_BASE_URL      API root, required for openai_compatible
  LIFECONTEXT_VAULT         vault folder (default: ~/LifeContext/vault)
  LIFECONTEXT_TIMEZONE      IANA time zone (default: the system's)
  LIFECONTEXT_API_KEY_FILE  file whose single line is the provider key
On a re-run, existing settings are kept; a key file given then replaces the
stored key.

Exit status: 0 installed and `lifecontext doctor` passed; 1 failed; 2 usage
error; 3 installed, but `lifecontext doctor` reported a problem (shown above).
EOF
}

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die() {
	printf 'Error: %s\n' "$*" >&2
	exit 1
}

download() {
	# HTTPS only, also after redirects.
	curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 -o "$2" "$1"
}

sha256_of() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | awk '{print $1}'
	elif command -v shasum >/dev/null 2>&1; then
		shasum -a 256 "$1" | awk '{print $1}'
	elif command -v openssl >/dev/null 2>&1; then
		openssl dgst -sha256 "$1" | awk '{print $NF}'
	else
		die "sha256sum, shasum or openssl is needed to verify downloads"
	fi
}

verify_sha256() {
	actual=$(sha256_of "$1")
	if [ "$actual" != "$2" ]; then
		die "checksum mismatch for $3 (expected $2, got $actual); nothing was installed from it"
	fi
}

tty_available() {
	(: </dev/tty) 2>/dev/null
}

detect_platform() {
	os=$(uname -s)
	arch=$(uname -m)
	case "$os/$arch" in
	Linux/x86_64 | Linux/amd64)
		uv_target="x86_64-unknown-linux-musl"
		uv_sha256=$UV_SHA256_LINUX_X86_64
		platform="linux-x86_64"
		;;
	Linux/aarch64 | Linux/arm64)
		uv_target="aarch64-unknown-linux-musl"
		uv_sha256=$UV_SHA256_LINUX_AARCH64
		platform="linux-aarch64"
		;;
	Darwin/arm64)
		uv_target="aarch64-apple-darwin"
		uv_sha256=$UV_SHA256_MACOS_ARM64
		platform="macos-arm64"
		;;
	Darwin/x86_64)
		if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = 1 ]; then
			die "this terminal runs under Rosetta; open a native (arm64) terminal and run the installer again"
		fi
		die "macOS on Intel is not supported (PyTorch ships no macOS x86_64 builds); use Docker instead"
		;;
	*)
		die "unsupported system $os $arch: LifeContext installs natively on Linux x86_64 and aarch64 and on macOS arm64; use Docker elsewhere"
		;;
	esac
	if [ "$os" = Linux ]; then
		if ldd --version 2>&1 | grep -qi musl; then
			die "musl-based Linux (for example Alpine) is not supported: PyTorch needs glibc; use Docker instead"
		fi
		# The embedded Postgres ships manylinux_2_35 wheels only.
		glibc=$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')
		if [ -n "$glibc" ] && ! printf '%s\n' "$glibc" | awk -F. '{exit !($1 > 2 || ($1 == 2 && $2 >= 35))}'; then
			die "glibc $glibc is too old: LifeContext needs glibc 2.35 or later (Ubuntu 22.04, Debian 12 or newer); use Docker instead"
		fi
	else
		macos=$(sw_vers -productVersion 2>/dev/null) || macos=""
		if [ -n "$macos" ] && [ "${macos%%.*}" -lt 14 ] 2>/dev/null; then
			die "macOS $macos is too old: LifeContext needs macOS 14 or later"
		fi
	fi
}

as_root() {
	if [ "$(id -u)" -eq 0 ]; then
		"$@"
	else
		sudo -n "$@"
	fi
}

can_run_as_root() {
	[ "$(id -u)" -eq 0 ] || { command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; }
}

ocr_ready() {
	command -v tesseract >/dev/null 2>&1 || return 1
	languages=$(tesseract --list-langs 2>/dev/null </dev/null) || return 1
	for language in eng rus ukr; do
		printf '%s\n' "$languages" | grep -qx "$language" || return 1
	done
}

install_uv() {
	if [ -x "$uv" ] && [ "$("$uv" --version 2>/dev/null | awk '{print $2}')" = "$UV_VERSION" ]; then
		say "uv $UV_VERSION is already in $data_dir/bin"
		return
	fi
	say "Downloading uv $UV_VERSION ($uv_target)"
	download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv_target.tar.gz" "$tmp/uv.tar.gz"
	verify_sha256 "$tmp/uv.tar.gz" "$uv_sha256" "uv $UV_VERSION"
	tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
	mkdir -p "$data_dir/bin"
	cp "$tmp/uv-$uv_target/uv" "$uv.new"
	chmod 755 "$uv.new"
	mv -f "$uv.new" "$uv"
	say "uv $UV_VERSION installed in $data_dir/bin (SHA-256 verified)"
}

# A cosign that can read the Sigstore bundle the release workflow writes (cosign 3).
cosign_usable() {
	command -v cosign >/dev/null 2>&1 || return 1
	major=$(cosign version 2>/dev/null | sed -n 's/^GitVersion:[[:space:]]*v\{0,1\}\([0-9][0-9]*\)\..*/\1/p' | head -n 1)
	if [ -z "$major" ] || [ "$major" -lt 3 ]; then
		warn "cosign ${major:-of unknown version} is older than 3.0 and cannot read this release's signature bundle; treating it as absent"
		return 1
	fi
}

# Check SHA256SUMS in $1 (signature when possible) and the wheel and lock against it.
verify_assets() {
	assets=$1
	wheel_name="lifecontext-$version-py3-none-any.whl"
	lock_name="requirements-$platform.txt"
	for name in SHA256SUMS "$wheel_name" "$lock_name"; do
		[ -f "$assets/$name" ] || die "$name is missing from the release assets in $assets"
	done
	identity="https://github.com/$REPOSITORY/.github/workflows/release.yml@refs/tags/v$version"
	issuer="https://token.actions.githubusercontent.com"
	if [ ! -f "$assets/SHA256SUMS.sigstore.json" ]; then
		warn "SHA256SUMS.sigstore.json is not in $assets, so the signature of SHA256SUMS was not checked"
	elif cosign_usable; then
		if errors=$(cosign verify-blob --bundle "$assets/SHA256SUMS.sigstore.json" \
			--certificate-identity "$identity" --certificate-oidc-issuer "$issuer" \
			"$assets/SHA256SUMS" </dev/null 2>&1 >/dev/null); then
			say "Signature of SHA256SUMS verified with cosign (signed by the release workflow of v$version)"
		else
			printf '%s\n' "$errors" >&2
			die "cosign could not verify SHA256SUMS of release $version (above); nothing was installed"
		fi
	else
		say "The signature of SHA256SUMS was not checked (needs cosign 3 or later). To check it:"
		say "  cosign verify-blob --bundle SHA256SUMS.sigstore.json \\"
		say "    --certificate-identity $identity \\"
		say "    --certificate-oidc-issuer $issuer SHA256SUMS"
		say "  (both files are assets of https://github.com/$REPOSITORY/releases/tag/v$version)"
	fi
	for name in "$wheel_name" "$lock_name"; do
		expected=$(awk -v name="$name" '$2 == name || $2 == "*" name {print $1}' "$assets/SHA256SUMS")
		[ -n "$expected" ] || die "SHA256SUMS of release $version does not list $name"
		verify_sha256 "$assets/$name" "$expected" "$name"
	done
	say "The wheel and $lock_name match SHA256SUMS"
	wheel_file="$assets/$wheel_name"
	lock_file="$assets/$lock_name"
}

fetch_release() {
	base="https://github.com/$REPOSITORY/releases/download/v$version"
	say "Downloading LifeContext $version from github.com/$REPOSITORY"
	mkdir -p "$tmp/release"
	for name in SHA256SUMS SHA256SUMS.sigstore.json "lifecontext-$version-py3-none-any.whl" "requirements-$platform.txt"; do
		download "$base/$name" "$tmp/release/$name"
	done
	verify_assets "$tmp/release"
}

# Stop a running service before its files change; start_service starts it again.
stop_running_service() {
	if [ "$os" = Linux ]; then
		if command -v systemctl >/dev/null 2>&1 &&
			systemctl --user is-active --quiet lifecontext.service </dev/null 2>/dev/null; then
			say "Stopping lifecontext.service for the upgrade"
			systemctl --user stop lifecontext.service </dev/null && service_stopped=1
		fi
	elif launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1 </dev/null; then
		say "Stopping the launchd agent for the upgrade"
		launchctl bootout "gui/$(id -u)/$LAUNCHD_LABEL" </dev/null && service_stopped=1
	fi
}

start_stopped_service() {
	[ "$service_stopped" -eq 1 ] || return 0
	service_stopped=0
	if [ "$os" = Linux ]; then
		systemctl --user start lifecontext.service </dev/null || warn "could not start lifecontext.service"
	elif ! launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1 </dev/null; then
		launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist" </dev/null ||
			warn "could not load the launchd agent"
	fi
}

backup_before_upgrade() {
	if [ "$no_backup" -eq 1 ]; then
		say "Not backing up (--no-backup)"
		return
	fi
	say "Backing up your data before the upgrade (lifecontext backup)"
	if report=$("$lc" backup --stop-service --json </dev/null); then
		backup_archive=$(printf '%s' "$report" | "$venv/bin/python" -I -c 'import json, sys; print(json.load(sys.stdin)["archive"])')
		say "Backup written: $backup_archive"
	else
		start_stopped_service
		die "the backup before the upgrade failed ($report); nothing was changed. Stop lifecontext serve if it runs, or pass --no-backup"
	fi
}

install_package() {
	export UV_PYTHON_INSTALL_DIR="$data_dir/python"
	export UV_PYTHON_BIN_DIR="$data_dir/python/bin"
	export UV_CACHE_DIR="$data_dir/cache"
	export UV_PYTHON_PREFERENCE=only-managed
	export UV_NO_CONFIG=1
	# Large wheels from the PyTorch index; tolerate a slow mirror.
	export UV_HTTP_TIMEOUT=120 UV_HTTP_RETRIES=5
	unset VIRTUAL_ENV CONDA_PREFIX PYTHONHOME PYTHONPATH 2>/dev/null || true
	fingerprint="$(sha256_of "$wheel_file") $(sha256_of "$lock_file")"
	if [ -x "$lc" ] && [ -f "$data_dir/installed.sha256" ] &&
		[ "$(cat "$data_dir/installed.sha256")" = "$fingerprint" ] &&
		"$lc" --version </dev/null >/dev/null 2>&1; then
		say "This wheel and lock are already installed ($("$lc" --version </dev/null))"
		return
	fi
	if [ -x "$lc" ] && [ -e "$HOME/.lifeweave/config.yaml" ]; then
		stop_running_service
		backup_before_upgrade
	fi
	if [ ! -x "$venv/bin/python" ] ||
		! "$venv/bin/python" -I -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' </dev/null 2>/dev/null; then
		rm -rf "$venv"
		say "Creating $venv with Python 3.12 from uv"
		"$uv" venv --python 3.12 "$venv" </dev/null
	fi
	rm -f "$data_dir/installed.sha256"
	incomplete="the installation in $venv is incomplete; your settings and data are untouched${backup_archive:+ and backed up in $backup_archive}. Run the installer again"
	set -- --python "$venv/bin/python" --require-hashes
	if [ "$os" = Linux ]; then
		# The lock names the PyTorch CPU index; its +cpu torch exists only there.
		set -- "$@" --index-strategy unsafe-best-match
	fi
	say "Installing the dependencies from $(basename "$lock_file"), every file checked against its hash (several minutes the first time)"
	"$uv" pip sync "$@" "$lock_file" </dev/null || die "uv could not install the locked dependencies (see above); $incomplete"
	say "Installing $(basename "$wheel_file")"
	"$uv" pip install --python "$venv/bin/python" --no-deps "$wheel_file" </dev/null ||
		die "uv could not install $(basename "$wheel_file") (see above); $incomplete"
	"$uv" pip check --python "$venv/bin/python" </dev/null ||
		die "the lock does not satisfy $(basename "$wheel_file") (uv pip check); $incomplete"
	printf '%s\n' "$fingerprint" >"$data_dir/installed.sha256"
	package_changed=1
	say "Installed $("$lc" --version </dev/null)"
}

# Fetch the models at their fixed revisions unless a cache already has them, so
# the first start runs offline. Hindsight loads the "main" reference.
seed_models() {
	"$venv/bin/python" -I -c '
import sys
from pathlib import Path

from huggingface_hub import constants, snapshot_download

models = ((sys.argv[1], sys.argv[2], ["*.json", "*.txt", "model.safetensors"]),
          (sys.argv[3], sys.argv[4], ["*.json", "*.model", "pytorch_model.bin"]))
hub = Path(constants.HF_HUB_CACHE)
for repo, revision, patterns in models:
    main = hub / ("models--" + repo.replace("/", "--")) / "refs" / "main"
    if main.is_file():
        print("%s: already in %s" % (repo, hub))
        continue
    snapshot_download(repo, revision=revision, allow_patterns=patterns)
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(revision)
    print("%s: revision %s" % (repo, revision[:12]))
' "$EMBEDDING_MODEL" "$EMBEDDING_REVISION" "$RERANKER_MODEL" "$RERANKER_REVISION" </dev/null ||
		warn "the models were not fetched now; the first start of the service downloads them"
}

link_command() {
	mkdir -p "$bin_dir"
	if [ -L "$link" ] && [ "$(readlink "$link")" = "$lc" ]; then
		say "$link already points at this installation"
	elif [ -e "$link" ] || [ -L "$link" ]; then
		warn "$link exists and is not this installation's link; left unchanged (the command is $lc)"
	else
		ln -s "$lc" "$link"
		say "Linked $link"
	fi
	case ":$PATH:" in
	*":$bin_dir:"*) ;;
	*) say "Add $bin_dir to PATH to run lifecontext by name, for example in ~/.profile:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
	esac
}

install_ocr() {
	if ocr_ready; then
		say "Tesseract with eng, rus and ukr is already installed"
		ocr_state="ready"
		return
	fi
	ocr_command=""
	if [ "$os" = Darwin ]; then
		ocr_command="brew install tesseract tesseract-lang"
		if command -v brew >/dev/null 2>&1; then
			say "Installing Tesseract with Homebrew"
			brew install tesseract tesseract-lang </dev/null || warn "brew could not install Tesseract"
		fi
	elif command -v apt-get >/dev/null 2>&1; then
		ocr_command="sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-ukr"
		if can_run_as_root; then
			say "Installing Tesseract with apt-get"
			{ as_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq &&
				as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
					tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-ukr; } </dev/null ||
				warn "apt-get could not install Tesseract"
		fi
	elif command -v dnf >/dev/null 2>&1; then
		ocr_command="sudo dnf install tesseract tesseract-langpack-eng tesseract-langpack-rus tesseract-langpack-ukr"
		if can_run_as_root; then
			say "Installing Tesseract with dnf"
			as_root dnf install -y -q tesseract tesseract-langpack-eng tesseract-langpack-rus \
				tesseract-langpack-ukr </dev/null || warn "dnf could not install Tesseract"
		fi
	fi
	if ocr_ready; then
		say "Tesseract with eng, rus and ukr is installed"
		ocr_state="ready"
	elif [ -n "$ocr_command" ]; then
		warn "OCR is not available yet, so scans and images stay unread (listed as unresolved) until you run:"
		say "  $ocr_command"
		ocr_state="missing: $ocr_command"
	else
		warn "no apt-get, dnf or brew found: install Tesseract with the eng, rus and ukr language data yourself"
		ocr_state="missing: install Tesseract with eng, rus and ukr"
	fi
}

print_init_report() {
	# The report holds the endpoint; print everything but the URL.
	"$venv/bin/python" -I -c '
import json, sys
report = json.loads(sys.stdin.read())
if "error" in report:
    print("lifecontext init failed: " + report["error"].replace("_", " "))
    sys.exit(1)
key = {"stored": "stored in .env", "replaced": "replaced in .env", "kept": "already in .env",
       "missing": "MISSING", "not_required": "not needed for this loopback endpoint"}
print("Settings: %s (config %s)" % (report["home"], report["config_state"]))
print("Model: %s %s" % (report["provider"], report["model"]))
print("API key: " + key.get(report["credentials_state"], report["credentials_state"]))
print("Vault: %s" % report["vault_root"])
print("Time zone: %s" % report.get("timezone"))
notes = {
    "flags_ignored": "kept the existing settings; not applied: %s (change them with lifecontext init --force)"
                     % ", ".join(report.get("ignored") or []),
    "existing_key_kept": "kept the stored key; replace it with lifecontext init --replace-key",
    "model_rejects_forced_tool_use": "this model rejects the forced tool call Hindsight uses, so retain fails",
}
for warning in report.get("warnings") or []:
    print("Note: " + notes.get(warning, warning.replace("_", " ")))
'
}

configure() {
	# init reads LIFECONTEXT_PROVIDER, _MODEL, _BASE_URL, _VAULT and the key file
	# LIFECONTEXT_API_KEY_FILE itself; only the time zone is passed as a flag.
	if [ -e "$HOME/.lifeweave/config.yaml" ]; then
		say "Existing settings in ~/.lifeweave kept (change them with lifecontext init --force)"
		if [ -n "${LIFECONTEXT_API_KEY_FILE:-}" ]; then
			env_file="$HOME/.lifeweave/.env"
			before=""
			[ ! -f "$env_file" ] || before=$(sha256_of "$env_file")
			report=$("$lc" init --non-interactive --replace-key --json </dev/null) || true
			printf '%s' "$report" | print_init_report || die "the key in LIFECONTEXT_API_KEY_FILE was not stored"
			if [ -f "$env_file" ] && [ "$(sha256_of "$env_file")" != "$before" ]; then
				key_changed=1
				say "The key from LIFECONTEXT_API_KEY_FILE replaced the stored one"
			fi
		fi
		return
	fi
	if [ -n "${LIFECONTEXT_PROVIDER:-}${LIFECONTEXT_MODEL:-}${LIFECONTEXT_API_KEY_FILE:-}" ]; then
		set -- init --non-interactive --json
		[ -z "${LIFECONTEXT_TIMEZONE:-}" ] || set -- "$@" --timezone "$LIFECONTEXT_TIMEZONE"
		report=$("$lc" "$@" </dev/null) || true
		printf '%s' "$report" | print_init_report || die "setup did not finish; fix the settings and run the installer again"
	elif [ "$interactive" != no ] && tty_available; then
		say "Answer a few questions; the API key is read with a hidden prompt."
		"$lc" init </dev/tty || die "lifecontext init failed; run it again with: lifecontext init"
	else
		say "Skipped: no terminal and no LIFECONTEXT_* settings. Run lifecontext init to finish the setup."
	fi
}

# Problems doctor reports that keep the service from starting.
setup_problems() {
	"$lc" doctor --json </dev/null 2>/dev/null | "$venv/bin/python" -I -c '
import json, sys
try:
    problems = json.loads(sys.stdin.read()).get("problems", [])
except ValueError:
    problems = ["doctor_failed"]
print(" ".join(p for p in problems if p.startswith(("config_", "credentials_", "home_", "vault_"))))
'
}

service_manager_available() {
	case "$os" in
	Linux) command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1 </dev/null ;;
	Darwin) launchctl print "gui/$(id -u)" >/dev/null 2>&1 </dev/null ;;
	*) return 1 ;;
	esac
}

api_answers() {
	"$venv/bin/python" -I -c '
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
' "http://127.0.0.1:$API_PORT/healthz" </dev/null
}

wait_for_api() {
	waited=0
	while [ "$waited" -lt "$HEALTH_WAIT_SECONDS" ]; do
		if api_answers; then
			return 0
		fi
		sleep 5
		waited=$((waited + 5))
	done
	return 1
}

start_service() {
	service_state="not installed"
	if [ "$no_service" -eq 1 ]; then
		start_stopped_service
		say "Skipped (--no-service). Run the API in a terminal with: lifecontext serve"
		return
	fi
	problems=$(setup_problems)
	if [ -n "$problems" ]; then
		say "Skipped until the setup is complete ($problems). Then run: lifecontext service install --maintenance"
		return
	fi
	if ! service_manager_available; then
		if [ "$os" = Linux ]; then
			say "No systemd user session here, so no service was installed. Run the API with: lifecontext serve"
			say "(or log in through a systemd session, run loginctl enable-linger \"\$USER\" and then lifecontext service install --maintenance)"
		else
			say "No launchd session for this user, so no agent was installed. Run the API with: lifecontext serve"
		fi
		return
	fi
	if ! "$lc" service install --maintenance </dev/null; then
		start_stopped_service
		warn "lifecontext service install failed; run the API with lifecontext serve, or see lifecontext service status"
		return
	fi
	if [ "$service_stopped" -eq 1 ]; then
		start_stopped_service
	elif [ "$package_changed" -eq 1 ] || [ "$key_changed" -eq 1 ]; then
		# A unit that did not change is not restarted by service install; load the new
		# code or key (the Hindsight daemon stops with the service and reads it on start).
		if [ "$os" = Linux ]; then
			systemctl --user restart lifecontext.service </dev/null || warn "could not restart lifecontext.service"
		else
			launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" </dev/null >/dev/null 2>&1 ||
				warn "could not restart the launchd agent"
		fi
	fi
	say "Waiting for the API on 127.0.0.1:$API_PORT"
	if wait_for_api; then
		service_state="running"
		say "The API answers"
	else
		service_state="installed, still starting"
		warn "the API did not answer within $HEALTH_WAIT_SECONDS s; check lifecontext service status and lifecontext doctor"
	fi
}

link_skills() {
	if [ "$no_skills" -eq 1 ]; then
		say "Skipped (--no-skills). Link it later with: lifecontext skills install"
		return
	fi
	"$lc" skills install </dev/null || warn "lifecontext skills install failed; see lifecontext skills check"
}

remove_units_directly() {
	if [ "$(uname -s)" = Darwin ]; then
		for label in "$LAUNCHD_LABEL.maintain" "$LAUNCHD_LABEL"; do
			plist="$HOME/Library/LaunchAgents/$label.plist"
			if [ -e "$plist" ]; then
				launchctl bootout "gui/$(id -u)/$label" </dev/null >/dev/null 2>&1 || true
				rm -f "$plist"
				say "Removed $plist"
			fi
		done
		return
	fi
	units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
	removed=0
	for unit in lifecontext-maintain.timer lifecontext-maintain.service lifecontext.service; do
		if [ -e "$units/$unit" ] || [ -L "$units/$unit" ]; then
			systemctl --user disable --now "$unit" </dev/null >/dev/null 2>&1 || true
			rm -f "$units/$unit"
			say "Removed $units/$unit"
			removed=1
		fi
	done
	for wants in "$units/default.target.wants/lifecontext.service" "$units/timers.target.wants/lifecontext-maintain.timer"; do
		if [ -L "$wants" ]; then
			rm -f "$wants"
		fi
	done
	if [ "$removed" -eq 1 ]; then
		systemctl --user daemon-reload </dev/null >/dev/null 2>&1 || true
	fi
}

remove_skill_links_directly() {
	for directory in .claude/skills .codex/skills .agents/skills .hermes/skills/productivity; do
		skill_link="$HOME/$directory/personal-journal-operations"
		if [ -L "$skill_link" ]; then
			case "$(readlink "$skill_link")" in
			*/agent_skill/personal-journal-operations)
				rm -f "$skill_link"
				say "Removed $skill_link"
				;;
			esac
		fi
	done
}

uninstall() {
	step "Removing the LifeContext installation"
	# Without a working command (a broken venv), remove the known files directly.
	if [ -x "$lc" ] && "$lc" --version </dev/null >/dev/null 2>&1; then
		"$lc" service uninstall </dev/null || warn "lifecontext service uninstall reported a problem (above)"
		"$lc" skills remove </dev/null || warn "lifecontext skills remove reported a problem (above)"
	else
		remove_units_directly
		remove_skill_links_directly
	fi
	if [ -L "$link" ] && [ "$(readlink "$link")" = "$lc" ]; then
		rm -f "$link"
		say "Removed $link"
	fi
	rm -rf "${data_dir:?}/venv" "${data_dir:?}/bin" "${data_dir:?}/python" "${data_dir:?}/cache"
	rm -f "${data_dir:?}/installed.sha256"
	rmdir "$data_dir" 2>/dev/null || true
	say "Removed $data_dir (uv, Python and the virtual environment)"
	say ""
	say "Kept, delete them yourself only if you want them gone:"
	say "  ~/.lifeweave         settings, the key in .env, custody archive, catalog, wiki revisions"
	say "  your vault           ~/LifeContext/vault unless you chose another folder"
	say "  ~/LifeContext/backups, ~/.hindsight, ~/.pg0 (the index database), ~/.cache/huggingface"
}

usage_error() {
	printf 'Error: %s\n' "$1" >&2
	exit 2
}

main() {
	action=install
	version=$LIFECONTEXT_RELEASE
	assets=""
	wheel=""
	lock=""
	interactive=auto
	no_service=0
	no_skills=0
	no_backup=0
	allow_root=0
	while [ $# -gt 0 ]; do
		case "$1" in
		--version | --assets | --wheel | --lock)
			[ $# -ge 2 ] || usage_error "$1 needs a value"
			case "$1" in
			--version) version=$2 ;;
			--assets) assets=$2 ;;
			--wheel) wheel=$2 ;;
			--lock) lock=$2 ;;
			esac
			shift 2
			;;
		--version=*) version=${1#*=} && shift ;;
		--assets=*) assets=${1#*=} && shift ;;
		--wheel=*) wheel=${1#*=} && shift ;;
		--lock=*) lock=${1#*=} && shift ;;
		--non-interactive) interactive=no && shift ;;
		--no-service) no_service=1 && shift ;;
		--no-skills) no_skills=1 && shift ;;
		--no-backup) no_backup=1 && shift ;;
		--allow-root) allow_root=1 && shift ;;
		--uninstall) action=uninstall && shift ;;
		-h | --help)
			usage
			exit 0
			;;
		*)
			printf 'Error: unknown option %s\n\n' "$1" >&2
			usage >&2
			exit 2
			;;
		esac
	done
	if { [ -n "$wheel" ] && [ -z "$lock" ]; } || { [ -z "$wheel" ] && [ -n "$lock" ]; }; then
		usage_error "--wheel and --lock go together"
	fi
	if [ -n "$wheel" ] && [ -n "$assets" ]; then
		usage_error "use --assets or --wheel with --lock, not both"
	fi
	case "$version" in
	[0-9]*) ;;
	*) usage_error "--version needs a release number such as 1.2.0" ;;
	esac
	case "$version" in
	*[!0-9A-Za-z.+-]*) usage_error "--version may contain only letters, digits, '.', '+' and '-'" ;;
	esac

	if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
		die "HOME is not set to a directory"
	fi
	if [ "$(id -u)" -eq 0 ] && [ "$allow_root" -eq 0 ]; then
		die "do not run the installer as root: LifeContext installs into your home directory and never needs root to run (use --allow-root to override)"
	fi
	data_dir="$HOME/.local/share/lifecontext"
	bin_dir="$HOME/.local/bin"
	venv="$data_dir/venv"
	uv="$data_dir/bin/uv"
	lc="$venv/bin/lifecontext"
	link="$bin_dir/lifecontext"

	if [ "$action" = uninstall ]; then
		uninstall
		exit 0
	fi

	detect_platform
	command -v curl >/dev/null 2>&1 || die "curl is needed to download uv and the release"
	tmp=$(mktemp -d "${TMPDIR:-/tmp}/lifecontext-install.XXXXXX")
	trap 'rm -rf "$tmp"' EXIT
	trap 'exit 1' HUP INT TERM
	package_changed=0
	key_changed=0
	service_stopped=0
	backup_archive=""
	ocr_state="unknown"
	service_state="not installed"

	step "Checking the release"
	if [ -n "$wheel" ]; then
		[ -f "$wheel" ] || die "--wheel $wheel is not a file"
		[ -f "$lock" ] || die "--lock $lock is not a file"
		case "$(basename "$wheel")" in
		lifecontext-*.whl) ;;
		*) die "--wheel must name a lifecontext-*.whl file" ;;
		esac
		wheel_file=$wheel
		lock_file=$lock
		say "Using the local wheel $(basename "$wheel") (SHA-256 $(sha256_of "$wheel"))"
		say "Using the lock $lock (SHA-256 $(sha256_of "$lock"))"
	elif [ -n "$assets" ]; then
		[ -d "$assets" ] || die "--assets $assets is not a directory"
		say "Using the release assets of LifeContext $version in $assets"
		verify_assets "$assets"
	else
		fetch_release
	fi

	step "Installing uv and Python 3.12 into $data_dir"
	install_uv
	step "Installing LifeContext"
	install_package
	link_command
	step "Fetching the embedding and rerank models (fixed revisions)"
	seed_models
	step "Installing Tesseract OCR (English, Russian, Ukrainian)"
	install_ocr
	step "Setting up ~/.lifeweave, the vault and the model key"
	configure
	step "Starting the API as a user service"
	start_service
	step "Linking the agent skill"
	link_skills
	step "Checking the installation"
	doctor_status=0
	"$lc" doctor </dev/null || doctor_status=$?

	step "Done"
	say "LifeContext: $("$lc" --version </dev/null) in $venv"
	say "Command: $link"
	say "Service: $service_state"
	say "OCR: $ocr_state"
	[ -z "$backup_archive" ] || say "Backup made before the upgrade: $backup_archive"
	if [ "$doctor_status" -ne 0 ]; then
		warn "lifecontext doctor reported a problem (listed above); fix it, then run lifecontext doctor again"
		exit 3
	fi
}

main "$@"
