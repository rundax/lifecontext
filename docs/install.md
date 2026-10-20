# Installing LifeContext

Three ways to install, each into your own account; running LifeContext never
needs root:

- [`install.sh`](#installsh) on Linux (x86_64 or aarch64, glibc 2.35 or later:
  Ubuntu 22.04, Debian 12 or newer) and macOS 14 or later on Apple silicon.
- [Docker](#docker) anywhere Docker runs, including Windows through Docker
  Desktop.
- From source, as in the [README](../README.md#quick-start).

Configuration after the install is in [operations.md](operations.md); what
leaves your machine is in [privacy.md](privacy.md).

## install.sh

```bash
curl -fsSL https://github.com/rundax/lifecontext/releases/latest/download/install.sh | sh
```

This command works once the repository is public and v1.2.0 is published with
its release assets. Until then, run the script from a checkout with a wheel
built from the tree and the lock for your platform (`linux-x86_64`,
`linux-aarch64` or `macos-arm64`):

```bash
git clone https://github.com/rundax/lifecontext.git
cd lifecontext
python3 -m venv /tmp/lifecontext-build
/tmp/lifecontext-build/bin/pip install build==1.6.1
/tmp/lifecontext-build/bin/python -m build --wheel
sh install.sh --wheel dist/lifecontext-*.whl --lock requirements/linux-x86_64.txt
```

To pass options through `curl | sh`, use `sh -s --`, for example
`curl -fsSL .../install.sh | sh -s -- --no-service`. The script needs `curl`.

### What it does

1. Checks the operating system, CPU and C library and stops with a clear
   message on anything else (Intel Macs, macOS before 14, musl-based Linux such
   as Alpine, glibc before 2.35, Windows: use Docker). It refuses to run as root
   unless you pass `--allow-root`.
2. Downloads `SHA256SUMS`, its signature bundle `SHA256SUMS.sigstore.json`, the
   wheel and the dependency lock for your platform
   (`requirements-linux-x86_64.txt`, `requirements-linux-aarch64.txt` or
   `requirements-macos-arm64.txt`) of the pinned release from GitHub over
   HTTPS, also after redirects, and checks the wheel and the lock against
   `SHA256SUMS`. With cosign 3 or later installed it first verifies that the
   release workflow of that tag signed `SHA256SUMS` (see
   [Verifying a release](#verifying-a-release)) and stops, showing cosign's
   message, if it did not. An older cosign cannot read the bundle and is treated
   as absent; without cosign, the script prints the command to check it
   yourself.
3. Installs uv 0.12.24 into `~/.local/share/lifecontext/bin` (the script pins
   its SHA-256), and uv installs Python 3.12 into
   `~/.local/share/lifecontext/python`. Neither touches a uv or Python you
   already have.
4. Creates `~/.local/share/lifecontext/venv` and installs exactly the
   dependencies the lock names, every file checked against its SHA-256
   (`uv pip sync --require-hashes`), then the wheel itself, then checks that
   they fit together (`uv pip check`). On Linux the lock takes CPU-only
   PyTorch from the PyTorch CPU index (about 2.4 GB instead of about 6 GB). It
   links `~/.local/bin/lifecontext` to the installed command, unless something
   else already has that name.
5. Fetches the embedding and rerank models at fixed revisions into the Hugging
   Face cache (about 0.6 GB), unless the cache already has them, so the
   service's first start makes no Hugging Face calls.
6. Installs Tesseract with English, Russian and Ukrainian through Homebrew on
   macOS, or through `apt-get`, `dnf` or `pacman` (Arch Linux) when `sudo`
   works without a password prompt; it never asks for a password. Otherwise it prints the exact command
   and goes on; scans and images stay unread (listed as unresolved) and
   `doctor` reports OCR as missing until you run it.
7. Sets up `~/.lifeweave` and the vault. In a terminal (this works under
   `curl | sh` too) it asks for the model endpoint: the provider (default
   `openai_compatible`), the API base URL (default `http://127.0.0.1:8317/v1`,
   where a local CLIProxyAPI usually listens), the model ID, the API key with a
   hidden prompt (leave it empty for an endpoint on this machine that needs
   none) and the vault folder. The key reaches `lifecontext init` through a
   private temporary file, never as an argument. Without a terminal it takes
   the [variables below](#unattended-install). Existing settings in
   `~/.lifeweave` are kept; change them later with
   [`lifecontext llm set`](#change-the-model-or-endpoint-later).
8. When the setup is complete and a systemd user session (Linux) or launchd
   (macOS) is available, runs `lifecontext service install --maintenance` and
   waits for the API to answer. Otherwise it tells you to run
   `lifecontext serve`.
9. Runs `lifecontext skills install` for the agents it finds (Claude Code,
   Codex, Gemini CLI, Hermes), then `lifecontext doctor`.

It ships no vision model and writes no `vision` block: vision stays off until
you configure it ([operations](operations.md)). It never prints your key.

### Options

| Option | Meaning |
|---|---|
| `--version VERSION` | Release to install (default: the one the script pins, 1.2.0); letters, digits, `.`, `+` and `-` only |
| `--assets DIR` | Use release assets already in `DIR` (as `gh release download` saves them) instead of downloading; they are checked the same way |
| `--wheel PATH --lock PATH` | Install a locally built wheel with a lock from `requirements/` instead of a release |
| `--non-interactive` | Never prompt; set up from the variables below only |
| `--no-service` | Do not install the systemd user service or launchd agent |
| `--no-skills` | Do not link the agent skill |
| `--no-backup` | Do not back up before upgrading an existing installation |
| `--allow-root` | Allow running as root (not recommended) |
| `--uninstall` | Remove what the script installed ([below](#uninstall)) |

Exit status: `0` installed and `doctor` passed; `1` failed; `2` usage error;
`3` installed, but `doctor` reported a problem (it is printed above).

### Unattended install

When any of `LIFECONTEXT_PROVIDER`, `LIFECONTEXT_MODEL` or
`LIFECONTEXT_API_KEY_FILE` is set, `init` runs without questions:

| Variable | Meaning |
|---|---|
| `LIFECONTEXT_PROVIDER` | Provider ID (default `anthropic`) |
| `LIFECONTEXT_MODEL` | Exact model ID (default `claude-sonnet-5` for Anthropic) |
| `LIFECONTEXT_BASE_URL` | API root, required for `openai_compatible` |
| `LIFECONTEXT_VAULT` | Vault folder (default `~/LifeContext/vault`) |
| `LIFECONTEXT_TIMEZONE` | IANA time zone (default: the system's) |
| `LIFECONTEXT_API_KEY_FILE` | A file whose single line is the provider key |

```bash
LIFECONTEXT_MODEL=claude-sonnet-5 LIFECONTEXT_API_KEY_FILE=~/anthropic-key.txt \
  sh install.sh --non-interactive
```

The key is read from the file into `~/.lifeweave/.env` (mode `0600`); it is
never passed on a command line. Without a terminal and without these
variables the script installs everything but skips `init`, the service and
the skill links and tells you to run `lifecontext init`.

### Change the model or endpoint later

```bash
lifecontext llm show                       # provider, model, base URL, whether a key is stored
lifecontext llm set --model MODEL          # change only what you name
lifecontext llm set --base-url http://127.0.0.1:8317/v1 --provider openai_compatible --model MODEL
lifecontext llm set --api-key-stdin < ~/new-key.txt
```

`llm show` never prints the key. `llm set` validates like `init`, keeps every
other setting, replaces the key only when you give one (on stdin, or from the
file named by `LIFECONTEXT_API_KEY_FILE`), and restarts the user service when it
runs; without a usable service manager it prints the command to run. Changing
the provider needs `--model` as well, and `openai_compatible` needs
`--base-url`.

### Upgrade

Run the same command again, or with `--version` for another release. When the
wheel or the lock changed, it stops the service, backs up your data with
`lifecontext backup --stop-service` (into `~/LifeContext/backups`; `--no-backup`
skips this), installs the new version, starts the service again and runs
`doctor`. If the backup fails it changes nothing and starts the service again;
if the install fails, it says so and leaves your settings, data and the backup
untouched, and running it again finishes the job. A re-run of the same version
changes nothing.

It keeps `~/.lifeweave` (settings, key, data), the vault and backups. When
`LIFECONTEXT_API_KEY_FILE` is set on a re-run, that key replaces the stored one
(`lifecontext init --replace-key`); other `LIFECONTEXT_*` settings are not
applied to an existing setup (use `lifecontext init --force`). There is no
schema migration step yet beyond what the service does when it starts.

### Uninstall

```bash
sh install.sh --uninstall
```

It removes the service units (`lifecontext service uninstall`), the skill
links that point at LifeContext (`lifecontext skills remove`), the link
`~/.local/bin/lifecontext` and `~/.local/share/lifecontext` (uv, Python, the
virtual environment). It keeps `~/.lifeweave` (settings, the key in `.env`,
custody archive, catalog), the vault, `~/LifeContext/backups`, `~/.hindsight`,
`~/.pg0` (the index database) and `~/.cache/huggingface`; delete those
yourself only if you want your data gone.

## Docker

The image is built from `python:3.12-slim` (Debian 12, pinned by digest) with
the dependencies from the same hashed locks as install.sh (`pip install
--require-hashes`, CPU-only PyTorch), Tesseract (English, Russian, Ukrainian)
and the embedding and rerank models at fixed revisions already inside, so it
starts with `HF_HUB_OFFLINE=1` and downloads no models. It runs as the unprivileged user `lifecontext` (uid
1000). The API listens on `127.0.0.1:8765` inside the container and no port
is published; you reach it with `docker exec` or the
[`lifecontext-docker`](#agents-and-the-lifecontext-docker-wrapper) wrapper.

Once v1.2.0 is published the image is `ghcr.io/rundax/lifecontext:1.2.0`.
Until then, build it from a checkout: `docker compose build` (or
`docker build -t lifecontext .`).

### Start with Docker Compose

`compose.yaml` in the repository root runs one container named `lifecontext`
with named volumes, the key as a secret, a health check on `/healthz`,
`restart: unless-stopped`, a 4 GB memory limit and no Linux capabilities.

```bash
mkdir -p secrets
(umask 077 && cat > secrets/lifecontext_api_key)   # paste the key, Enter, Ctrl-D
sudo chown 1000 secrets/lifecontext_api_key        # Linux, if your uid is not 1000
cat > .env <<'EOF'
LIFECONTEXT_PROVIDER=anthropic
LIFECONTEXT_MODEL=claude-sonnet-5
LIFECONTEXT_TIMEZONE=Europe/Vienna
EOF
docker compose up -d
docker compose ps                                   # healthy after the first start
docker compose exec lifecontext lifecontext doctor
```

On the first start the entrypoint runs `lifecontext init --non-interactive
--vault /data/vault` with these variables and reads the key from the secret
(`/run/secrets/lifecontext_api_key`). The variables matter only for that first
start; later starts keep the settings in the home volume. The key is stored
again from the secret on every start (`init --replace-key`), so to change it,
update the secret file and run `docker compose restart`. Without them the
container waits until you run
`docker exec -it lifecontext lifecontext init --vault /data/vault`.

| Variable (shell or `.env`) | Meaning |
|---|---|
| `LIFECONTEXT_PROVIDER`, `LIFECONTEXT_MODEL`, `LIFECONTEXT_BASE_URL`, `LIFECONTEXT_TIMEZONE` | `init` settings for the first start |
| `LIFECONTEXT_API_KEY_FILE` | Host path of the key file (default `./secrets/lifecontext_api_key`); it must be readable by uid 1000 |
| `LIFECONTEXT_IMAGE` | Image to run (default `ghcr.io/rundax/lifecontext:1.2.0`) |
| `LIFECONTEXT_CONTAINER` | Container name (default `lifecontext`) |
| `LIFECONTEXT_MEMORY_LIMIT` | Memory limit (default `4g`) |

Without Compose, pass the same: a read-only key file at
`/run/secrets/lifecontext_api_key` (or `-e LIFECONTEXT_API_KEY_FILE=PATH`), the
`LIFECONTEXT_*` variables, and volumes for `/data/home` and `/data/vault`. The
variable `LIFECONTEXT_API_KEY` also works but is less private: anyone who can
run `docker inspect` sees it.

### Volumes

- `/data/home`: settings and the key (`.lifeweave`), custody archive, catalog,
  the index database (`.pg0`), backups (`LifeContext/backups`).
- `/data/vault`: the Obsidian vault.

Compose uses named volumes. To open the vault in Obsidian on the host, mount a
folder you created that belongs to uid 1000 instead (`- ./vault:/data/vault`),
or build the image for your account with
`--build-arg LIFECONTEXT_UID=$(id -u) --build-arg LIFECONTEXT_GID=$(id -g)`.
A folder that belongs to another user is refused with `unsafe_vault`.

### Agents and the lifecontext-docker wrapper

`bin/lifecontext-docker` runs `lifecontext` in the container with
`docker exec`, passing stdin and stdout through:

```bash
bin/lifecontext-docker status --json
ln -s "$PWD/bin/lifecontext-docker" ~/.local/bin/lifecontext
```

Linked as `lifecontext` on your `PATH`, it lets the agent skill work
unchanged. Files on the host named by `retain --input`, `curate --input`,
`categories propose --input`, `documents import` and `import` are streamed
into a private directory in the container under the same name and removed
afterwards; `documents download` and `export` write their file on the host
(never over an existing one). Other paths, such as those of `backup`,
`restore` and `mcp --import-dir`, are paths in the container.
`LIFECONTEXT_CONTAINER` selects another container. An interrupted command
removes its staged files.

The wrapper streams a file through `docker exec` instead of mounting a host
folder read-only into the container. A mount would have to be set up before
the container starts, would show the container every file in that folder, and
the copies in it would have to be readable by uid 1000, so on most Linux hosts
by every user. Streaming hands the container only the one file named in the
command, into a directory only the container user can read.

Link the skill on the host yourself, since `skills install` in the container
links only inside it: `ln -s "$PWD/skills/personal-journal-operations"
~/.claude/skills/` (or the Claude Code plugin from the README). For MCP
clients, use the wrapper's absolute path as the command and `mcp` as the
argument; see [agents.md](agents.md#mcp-server).

### Stop, back up and restore

`docker compose stop` stops the API and then the Hindsight memory engine and
its database cleanly (allow up to 90 seconds). Back up from a one-off
container while the service is stopped:

```bash
docker compose stop
docker compose run --rm --no-deps lifecontext backup --json
docker compose run --rm --no-deps -T --entrypoint cat lifecontext \
  /data/home/LifeContext/backups/NAME.tar.gz > NAME.tar.gz
docker compose start
```

To restore into new, empty volumes (here a second project), stream the
archive in, then start it; the entrypoint stores the key from the secret,
since backups never contain it. `--vault /data/vault` is needed because the
vault volume lies outside the home volume (otherwise restore refuses with
`vault_outside_home`):

```bash
docker compose -p lifecontext-restored run --rm --no-deps -T --entrypoint sh lifecontext \
  -c 'cat > /tmp/backup.tar.gz && lifecontext restore /tmp/backup.tar.gz --vault /data/vault' < NAME.tar.gz
LIFECONTEXT_CONTAINER=lifecontext-restored docker compose -p lifecontext-restored up -d
```

### Upgrade

Set `LIFECONTEXT_IMAGE` to the new tag (or rebuild) and run
`docker compose up -d`; the volumes keep your data. Back up first.

## Verifying a release

Each release publishes the wheel and sdist, `install.sh`,
`constraints-cpu.txt`, the dependency locks `requirements-linux-x86_64.txt`,
`requirements-linux-aarch64.txt` and `requirements-macos-arm64.txt`,
`SHA256SUMS` over all of them, and `SHA256SUMS.sigstore.json`, a Sigstore
bundle from the release workflow, signed without a long-lived key. With
[cosign](https://docs.sigstore.dev/cosign/system_config/installation/) 3 or
later (2.x cannot read this bundle format):

```bash
cosign verify-blob --bundle SHA256SUMS.sigstore.json \
  --certificate-identity https://github.com/rundax/lifecontext/.github/workflows/release.yml@refs/tags/v1.2.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
```

The image is signed the same way:

```bash
cosign verify ghcr.io/rundax/lifecontext:1.2.0 \
  --certificate-identity https://github.com/rundax/lifecontext/.github/workflows/release.yml@refs/tags/v1.2.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```
