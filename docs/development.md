# Development

How to work on LifeContext from a checkout: the private `.dev/` environment, the
dev launcher, tests and CI. User-facing operation is in
[operations.md](operations.md).

## Local development files

All development state lives under `.dev/`, which is ignored by Git:

```text
lifecontext/             # the checkout
├── README.md
├── docs/
├── pyproject.toml
├── src/lifecontext/     # runtime
├── tests/               # unit tests
└── .dev/                # ignored local-only files
    ├── home/
    │   ├── .lifeweave/  # config.yaml and .env
    │   └── .hindsight/  # LifeContext profile and database
    ├── vault/Journal/   # stable managed Markdown projection
    ├── tmp/             # local build/test scratch
    ├── cache/           # local package cache
    └── venv/            # development environment
```

The local process `HOME` is `.dev/home`, so the files to edit are:

- `.dev/home/.lifeweave/config.yaml`
- `.dev/home/.lifeweave/.env`

The local Hindsight profile and database live under `.dev/home/.hindsight/`
(and the embedded PostgreSQL under `.dev/home/.pg0/`). This state is separate
from Hermes and from any normal user profile.

Keep directories mode `0700` and config/credential files mode `0600`. Never
put an API key in tracked files, command arguments, or chat. LifeContext
ignores `HERMES_HOME`, Hermes credentials, and ambient credential variables.

From the project root, create or refresh the development environment:

```bash
mkdir -m 700 -p .dev/home/.lifeweave .dev/tmp .dev/cache
chmod 700 .dev .dev/home .dev/home/.lifeweave .dev/tmp .dev/cache
python3 -m venv .dev/venv
HOME="$PWD/.dev/home" TMPDIR="$PWD/.dev/tmp" \
  UV_CACHE_DIR="$PWD/.dev/cache" \
  uv pip install --python .dev/venv/bin/python -c constraints-cpu.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match -e .
chmod 600 .dev/home/.lifeweave/config.yaml .dev/home/.lifeweave/.env
```

The pinned `hindsight-all` dependency uses several GB; `.dev/venv` is about
6 GB with the default CUDA build of PyTorch. `constraints-cpu.txt` at the
repository root pins `torch==2.14.0+cpu` on Linux; used with
`-c constraints-cpu.txt --extra-index-url https://download.pytorch.org/whl/cpu`
(pip or `uv pip`) it installs the CPU-only wheel and avoids the CUDA packages.
`uv pip` also needs `--index-strategy unsafe-best-match`: by default uv takes
each package only from the first index that has it, and the PyTorch index
carries too-old copies of `urllib3` and `numpy`. CI installs with pip and
resolves the uv command against the built wheel (the wheel job), and the
README's from-source steps install the same way. On macOS the pin does not
apply (PyPI's macOS wheels are CPU-only already). Keep the pin and
`TORCH_VERSION` in `.github/workflows/ci.yml` equal; CI checks it.

## Dependency locks

`install.sh` and the Docker image do not resolve dependencies: they install
exactly what `requirements/linux-x86_64.txt`, `requirements/linux-aarch64.txt`
or `requirements/macos-arm64.txt` pins, every file checked against its SHA-256
(`uv pip sync --require-hashes`, `pip install --require-hashes`). The locks
come from `pyproject.toml` and `constraints-cpu.txt` through
`requirements/compile.sh`, which runs `uv pip compile` for each platform
(manylinux 2.35 and macOS 14, the oldest the embedded Postgres wheels support).
After changing a dependency, run it with the uv that install.sh pins and commit
the result:

```bash
sh requirements/compile.sh          # keeps every pin it can, updates what changed
sh requirements/compile.sh --check  # what CI runs: fails when a lock is stale
```

To move all transitive dependencies to their newest allowed versions, delete
the three files first. The release workflow publishes them as
`requirements-PLATFORM.txt` next to the wheel and lists them in `SHA256SUMS`.

## Dev launcher

After the environment is installed, use `bin/lifecontext-dev` by absolute
path or add `bin` to your `PATH`. It works from any directory and preserves
relative input paths and piped stdin:

```bash
/path/to/lifecontext/bin/lifecontext-dev serve
/path/to/lifecontext/bin/lifecontext-dev status --json
/path/to/lifecontext/bin/lifecontext-dev reconcile --json
printf 'A dev journal note' | /path/to/lifecontext/bin/lifecontext-dev retain src_dev_note 1 --input -
```

The launcher uses this checkout's `.dev/venv`, fixes `HOME` to `.dev/home`,
sets dev temporary/XDG paths, and pins the API to loopback port **18765**.
Port overrides (including argparse abbreviations) are rejected. The standalone
runtime owns the fixed `lifeweave` Hindsight profile under that home; no Hermes
profile variables are needed. Start the dev service with this wrapper too.
Retain and correction project immutable versioned Markdown automatically.
Reconciliation verifies the actual filesystem and preserves conflicting edits.

Before invoking any command, it requires existing owner-owned `0700` dev,
home, application-home, temporary, vault, and Journal directories, a safe
`0600` config, and the exact `.dev/vault` / `.dev/vault/Journal` config pair.
Missing paths, symlinks, unsafe permissions, or config drift fail without
creating or repairing paths. Vault validation uses the runtime's managed
vault authority. It does not start a service implicitly. Port 18765 must remain
reserved for the dev service; clients select the running API by port, not HOME.

`bin/life-context-dev` is a symlink to the same launcher, kept for existing
service units and scripts.

## Startup checks in the dev home

From the project root:

```bash
HOME="$PWD/.dev/home" .dev/venv/bin/lifecontext preflight
HOME="$PWD/.dev/home" .dev/venv/bin/lifecontext preflight --retain-smoke
```

What the checks do is described in
[operations.md](operations.md#startup-checks).

## Tests

Run the unit suite with the development environment:

```bash
HOME="$PWD/.dev/home" TMPDIR="$PWD/.dev/tmp" \
  .dev/venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Tests keep their scratch files under `.dev/tmp` (`tests/local_paths.py`
creates it, mode `0700`, and refuses a symlinked or foreign-owned directory).
CI runs the same modules from the `tests/` directory with
`PYTHONPATH=src:tests`.

The wheel-install isolation test (`tests/test_install_isolation.py`) builds the
wheel and installs the pinned Hindsight stack in a second temporary
environment; it takes several minutes and several GB. On a small host, run it
in CI rather than locally.

Lint with `ruff check src tests scripts .github/scripts`. CI pins ruff 0.16.10,
and `[tool.ruff.lint]` in `pyproject.toml` spells out that version's default
rule set, so other ruff versions check the same rules. The existing findings
are known; the lint job is not blocking until they are cleaned up.

### End-to-end service verification

The opt-in test exercises the installed CLI against a real `lifecontext serve`
process and the real disposable Hindsight profile. It checks service readiness,
retains a synthetic source, replays the same idempotency key, checks the completed
operation, and verifies exact-version evidence by checksum. It binds the API to a
free `127.0.0.1` port and refuses to run unless both configured vault paths are
inside `.dev`. It uses the private config and key under `.dev/home`, writes a
temporary synthetic input under `.dev/tmp`, and leaves the resulting synthetic
record in the disposable archive, catalog, and Hindsight profile. The API process
stops after the test; the Hindsight profile may remain running for reuse, as it
does after `preflight`.

Run it from the project root after setting up `.dev/` as described above. Stop any
existing `lifecontext serve` process using `.dev/home` first; the service holds an
exclusive claim on its HOME:

```bash
LIFEWEAVE_E2E=1 HOME="$PWD/.dev/home" TMPDIR="$PWD/.dev/tmp" \
  .dev/venv/bin/python -m unittest discover -s tests -p 'test_cli_e2e.py' -v
```

This test makes a real retain request to the configured LLM endpoint using only
synthetic text. The provider may charge for that request. The ordinary test suite
skips it unless `LIFEWEAVE_E2E=1` is set. It reads the key from the mode-`0600`
`.dev/home/.lifeweave/.env` file and never prints the key.

For a manual readiness check in the dev home, start the service in one
terminal and query it from a second one:

```bash
HOME="$PWD/.dev/home" TMPDIR="$PWD/.dev/tmp" \
  .dev/venv/bin/lifecontext serve --port 18765
HOME="$PWD/.dev/home" .dev/venv/bin/lifecontext status --port 18765 --json
```

To exercise the provider-backed CLI path manually, use a synthetic file and a
new source ID. `retain` may incur provider usage; then use the returned operation
ID with `operations get` and verify its evidence:

```bash
printf '%s\n' 'Synthetic LifeContext smoke test. No personal data.' \
  > .dev/tmp/lifeweave-manual-smoke.md
chmod 600 .dev/tmp/lifeweave-manual-smoke.md
HOME="$PWD/.dev/home" TMPDIR="$PWD/.dev/tmp" \
  .dev/venv/bin/lifecontext retain src_manual_smoke_01 1 \
  --input .dev/tmp/lifeweave-manual-smoke.md \
  --idempotency-key manual-smoke-01 --port 18765 --json
OPERATION_ID="op_<paste-id-from-retain-output>"
HOME="$PWD/.dev/home" .dev/venv/bin/lifecontext operations get "$OPERATION_ID" \
  --port 18765 --json
HOME="$PWD/.dev/home" .dev/venv/bin/lifecontext evidence src_manual_smoke_01 \
  --version 1 --port 18765 --json
```

Stop the API with Ctrl+C in its terminal. A retain writes a synthetic record to
`.dev/home`; use a fresh source ID and idempotency key for each new manual run.

The current unit-test count is in each CI run's log. The provider-backed E2E
last recorded a pass on 2026-09-25 (version 1.1.0): ready status, idempotent
retain replay, completed operation, and exact-version evidence with a matching
checksum, from synthetic input whose record remains in the disposable `.dev`
data.

## Continuous integration

GitHub Actions workflows live in `.github/workflows/`:

- `ci.yml`, on every push and on pull requests from forks: `ruff` over `src`,
  `tests`, `scripts` and `.github/scripts` (not blocking yet); the unit suite
  on Ubuntu 24.04 with Python 3.12, Tesseract (`eng`, `rus`, `ukr`) and
  CPU-only PyTorch, in `.dev/venv`; an sdist and wheel build whose contents and
  license metadata are checked (`.github/scripts/check_dist.py`) and uploaded
  as the `lifecontext-dist` artifact. The install-isolation test runs only on
  manual dispatch and weekly.
- `spike-cpu-torch.yml`, manual: installs the package with CPU-only PyTorch into
  a fresh venv on Ubuntu 24.04 and, when asked, macOS 14, and records the venv
  size, package count, install and import times in the run summary.
- `ci.yml` also checks the dependency locks against `pyproject.toml`
  (`requirements/compile.sh --check`).
- `qualify.yml`, manual (`gh workflow run qualify.yml --ref BRANCH`, input
  `macos` adds macOS 14): builds the wheel, installs it with `install.sh` on
  a clean Ubuntu 24.04 runner and builds and starts the Docker image with
  Compose, then runs a small synthetic scenario against the CI model (repository
  secrets `LIFECONTEXT_CI_LLM_API_KEY` and `LIFECONTEXT_CI_LLM_BASE_URL`,
  variable `LIFECONTEXT_CI_LLM_MODEL`): capture, documents with OCR, cited
  recall, evidence, the compiled wiki, skills, MCP, backup and restore,
  re-install and uninstall, and for Docker no published port, a non-root user,
  recreate and restore. Each job uploads its evidence (`qualify-evidence-*`:
  versions, timings, sizes, check results) only after a scan for the endpoint
  and key. One run at a time; the model gateway is shared, so do not dispatch
  it in a loop. `.github/scripts/qualify.py` is the scenario. With
  `release_tag` it rehearses a release instead: it downloads that draft
  release's assets, runs its `install.sh --assets` (checksums and cosign
  signature included) and pulls and verifies the image.
- `release.yml`, on `v*` tags (a manual run is a dry run): see
  [Releasing](#releasing).
- `pages.yml`, manual: deploys `site/` from `main` to GitHub Pages, only while
  the repository is public. The release workflow will start it with
  `gh workflow run pages.yml --ref main`.
- `.github/dependabot.yml` proposes action updates weekly.

Actions are pinned to full commit SHAs; update the SHA and its version comment
together.

## Releasing

1. Set the version in `pyproject.toml`, `LIFECONTEXT_RELEASE` in `install.sh`
   and the image tag in `compose.yaml` (and the plugin manifests) to the same
   number, and make sure CI is green.
2. Push the tag `vVERSION`. `release.yml` checks that every file names that
   version, builds the wheel, sdist and assets without write access, signs
   `SHA256SUMS` keylessly with cosign, builds, pushes and signs the
   linux/amd64 and linux/arm64 image with an SBOM and provenance, and creates
   a draft release. Only for a final version (no letters in it) does a last
   job publish the draft, after the image is pushed and signed; a pre-release
   such as `1.2.0rc1` stays a draft.
3. Check the draft and the image: `gh workflow run qualify.yml --ref main -f
   release_tag=vVERSION` installs from the draft's assets and verifies both
   signatures.
4. In the repository settings, turn on immutable releases, so a published
   release's tag and assets can no longer change.
5. The GHCR package `lifecontext` stays private while the repository is; make
   it public in its package settings after the repository goes public.

## Local LLM route through a private CA

If your provider is reachable only over TLS with a private CA,
`scripts/dev_llm_forwarder.py --upstream https://<host>:<port> --ca-file <CA certificate>`
exposes it on `http://127.0.0.1:18317/v1`. It forwards only models and chat
completions requests, verifies the upstream certificate and hostname, and logs
no request content; set that URL as `base_url` in the dev config.

After a change to the dev LLM config or key, stop the API with Ctrl-C, stop
only the dev `lifeweave` profile so the next start regenerates its private
environment, and restart `lifecontext serve` through the launcher. Never stop
another Hindsight profile.

```bash
HOME="$PWD/.dev/home" .dev/venv/bin/python -c \
  'from hindsight_embed.daemon_client import stop_daemon; stop_daemon(profile="lifeweave")'
```

## Acceptance and sync handoff scripts

```bash
bin/lifecontext-dev reconcile --json
.dev/venv/bin/python scripts/dev_acceptance.py
.dev/venv/bin/python scripts/sync_handoff.py
```

The live acceptance script adds one clearly synthetic source and a correction
to dev custody and exercises two shell callers outside the repository. The
handoff command writes `.dev/sync-handoff.json` with private permissions,
paths and Markdown hashes only, and refuses missing/edited/unresolved output.
It does not enable any vault sync. Keep this private artifact out of commits.

Agents use the dev instance through the `personal-journal-operations` skill
with `CLI` pointed at `bin/lifecontext-dev`; see [agents.md](agents.md). The
skill lives in `src/lifecontext/agent_skill/` (the root
`skills/personal-journal-operations` is a relative link to it, so existing agent
links that point at the repository path keep working). Run
`scripts/install_agent_skill.sh` once after cloning (it runs
`.dev/venv/bin/lifecontext skills install --agent all` against the real HOME), and
`scripts/install_agent_skill.sh --check` to detect a missing link or a skill copied
over a link.

## Compatibility identifiers

The distribution, command and Python import are `lifecontext`, and the dev
launcher is `bin/lifecontext-dev`. The `life-context` command remains an alias
of `lifecontext`, and `bin/life-context-dev` is a symlink to the dev launcher
for existing service units and scripts.

These identifiers are written to stored data, sent over HTTP or used by tools
outside this repository, so they intentionally keep their legacy names:

- `~/.lifeweave` storage (`config.yaml`, `.env`, `catalog.sqlite3`, `archive/`,
  `derived/`, `lifeweave.lock`), the `LIFEWEAVE_LLM_API_KEY` credential name,
  and catalog table, column and operation-kind names.
- The `lifeweave` Hindsight profile and bank, and the disposable startup smoke
  bank, document ID and content.
- Managed vault and wiki markers `<!-- life-context:managed:start -->`,
  `<!-- life-context:managed:end -->`, `<!-- life-context:user:start -->` and
  `<!-- life-context:user:end -->`.
- Ownership markers `life-context-wiki`, `lifeweave-journal` and
  `lifeweave-relationship`, and the extractor name `life-context-extraction`.
- The `lifeweave.bundle` format, the `application/vnd.lifeweave.bundle` media
  type and the `X-Lifeweave-Operation-ID` header.
- The `LIFEWEAVE_E2E` test switch, the skill backup directory
  `${XDG_DATA_HOME:-~/.local/share}/life-context/skill-backups`, and the
  `personal-journal-operations` skill name.

They are not a second state store and must not be renamed in existing data.
