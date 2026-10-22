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
- `qualify.yml`, manual (`gh workflow run qualify.yml --ref BRANCH`), the full
  scenario: builds the wheel, installs it with `install.sh` on a clean Ubuntu
  24.04 runner (and on macOS 14 with `-f macos=true`) and builds and starts the
  Docker image with Compose, then runs a small synthetic scenario against a
  real model: capture, documents with OCR, cited recall, evidence, the
  compiled wiki, skills, MCP, backup and restore, re-install, upgrade and
  uninstall, and for Docker no published port, a non-root user, recreate and
  restore. The native install must end with the three next steps. A
  `wrong_key` job installs with a key the provider rejects and expects exit 3,
  "the provider rejected the key" and the `lifecontext llm set` command.
  Every job takes the model from two inputs:
  - `-f provider=openai_compatible` (the default) goes through the CI gateway:
    repository secrets `LIFECONTEXT_CI_LLM_API_KEY` and
    `LIFECONTEXT_CI_LLM_BASE_URL`, and the model from the variable
    `LIFECONTEXT_CI_LLM_MODEL` unless `-f model=ID` names another.
  - `-f provider=anthropic -f model=claude-sonnet-5` talks to Anthropic's own
    API with the secret `LIFECONTEXT_CI_ANTHROPIC_API_KEY`. No base URL is set,
    and the model input is required.

  Each job records the provider, the model and whether a base URL is set, read
  back with `lifecontext llm show`. It uploads its evidence
  (`qualify-evidence-*`: versions, timings, sizes, check results) only after a
  scan for the gateway endpoint and every CI key, which a final job repeats
  over all of it. One run at a time; the gateway is shared, so do not dispatch
  it in a loop. `.github/scripts/qualify.py` is the scenario, and
  `.github/scripts/ci-model.sh` picks the key. Two inputs replace the full
  scenario, and `macos` changes nothing with either:
  - `release_tag=vVERSION` checks a draft release: it downloads the draft's
    assets with the workflow token, runs its `install.sh --assets` (checksums
    and cosign signature included), runs doctor, pulls and verifies the image,
    and records the GHCR package's visibility.
  - `public_tag=vVERSION` checks a published release the way a new user
    installs it, with no token, on `ubuntu-24.04` and `macos-14`: `curl -fsSL
    …/releases/download/vVERSION/install.sh | sh -s -- --non-interactive`, or
    `…/releases/latest/download/install.sh` for a final, then doctor and
    `preflight --model-probe`. For a final it also pulls the image anonymously
    and runs `cosign verify` on it.
- `release.yml`, on `v*` tags: see [Releasing](#releasing). A manual run
  (`gh workflow run release.yml --ref BRANCH`) is a dry run: it builds and
  checks the wheel, the assets and the two-platform image, and signs, pushes
  and publishes nothing.
- `pages.yml`, manual: deploys `site/` from `main` to GitHub Pages. Nothing
  starts it on its own; on the day of a final release the maintainer runs
  `gh workflow run pages.yml --ref main` after the release is published (step
  12 of [Releasing](#releasing)). Before that, the site's install commands
  would fail.
- `.github/dependabot.yml` proposes action updates weekly.

Actions are pinned to full commit SHAs; update the SHA and its version comment
together.

## Releasing

The maintainer approves every tag, every publish and every settings change.
`VERSION` below is the release number, for example `1.2.0rc1` or `1.2.0`; a
version with letters in it is a pre-release.

1. Cut the branch `release/vVERSION` from `main`.
2. Write `VERSION`, identical in all five files: `version` in
   `pyproject.toml`, `__version__` in `src/lifecontext/__init__.py`,
   `"version"` in `src/lifecontext/agent_skill/.claude-plugin/plugin.json`,
   `LIFECONTEXT_RELEASE` in `install.sh` and the image tag in `compose.yaml`.
   `release.yml` compares the tag with three of them as plain strings, so
   `1.2.0rc1` is never written `1.2.0-rc1`. For a final, the release commit
   also replaces `# Unreleased` in `CHANGELOG.md` with
   `# VERSION (YYYY-MM-DD)`.
3. Push the branch; `ci.yml` must be green on it.
4. Run the full qualification on the branch:
   `gh workflow run qualify.yml --ref release/vVERSION -f macos=true`. Every
   job must pass.
5. Check that immutable releases are on (Settings, General, Releases). A
   release published afterwards can no longer change its tag or assets;
   drafts stay editable.
6. Tag the branch head and push the tag: `git tag vVERSION` and
   `git push origin vVERSION`.
7. `release.yml` runs on the tag. It checks that the tag, `pyproject.toml`,
   `install.sh` and `compose.yaml` name the same version. It builds and checks
   the wheel, the sdist and the assets with their `SHA256SUMS`, with no write
   access, and signs `SHA256SUMS` with cosign, keyless. It builds, pushes and
   signs the linux/amd64 and linux/arm64 image with an SBOM and provenance:
   `:VERSION`, and `:latest` for a final, which takes up to 150 minutes. Only
   then does it create the release as a **draft**. Every release stays a
   draft, a final too; nothing publishes it on its own.
8. Check the draft: `gh workflow run qualify.yml --ref main -f
   release_tag=vVERSION`. It must pass, and the `package_visibility` it records
   must be `private`: the image package stays private until a final is
   published (see "If a check of the draft fails" below).
9. Publish the draft. For a pre-release, run `gh release edit vVERSION
   --draft=false --prerelease`. A pre-release never becomes "latest", so
   `releases/latest/download/install.sh` keeps pointing at the last final. For
   a final, run `gh release edit vVERSION --draft=false --latest`.
10. For a final, make the GHCR package `rundax/lifecontext` public in its
    package settings. Do this before step 11, which pulls the image
    anonymously.
11. Check the public install: `gh workflow run qualify.yml --ref main -f
    public_tag=vVERSION`. For a pre-release this installs from the release's
    own URL. For a final it installs through `releases/latest` and also pulls
    and verifies the image.
12. For a final, deploy the site: `gh workflow run pages.yml --ref main`.
13. For a final, merge back into `main`: the `CHANGELOG.md` heading, and the
    versions by the rule below. A pre-release is not merged back. Its branch
    changes only the version; fixes land on `main` and go out in a new branch
    and tag.

**Versions on `main`.** After a final release, `pyproject.toml`,
`src/lifecontext/__init__.py` and `plugin.json` carry the next development
version (`1.2.1.dev0` after `1.2.0`). `install.sh` and `compose.yaml` keep
naming the latest published release (`1.2.0`): an install from a checkout
fetches that release, and the README's Docker quick start pulls that image.

**If a check of the draft fails** (step 8): delete the draft and the tag (a
draft is not immutable), fix on `main`, cut the branch again and tag the same
version. The image tags the failed run pushed are overwritten by the next run
before anyone can pull them, because the package is still private. If step 8
records the package as public, set it to private before going on.

**Dry run.** `gh workflow run release.yml --ref BRANCH` runs the build and
image jobs without signing, pushing or creating a release; use it after a
change to `release.yml` or the Dockerfile.

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
