# LifeContext

**Your documents, compiled. Every claim cited.**

Give Claude Code, Codex and your other agents your contracts, letters, scans
and notes. LifeContext keeps the files on your machine, sends their text only
to the model you choose, and cites every claim with the exact quote, its page
or passage, and the source version, so you can check it.

For people who work with AI agents and keep documents that matter: leases,
contracts, insurance, tax and medical letters, research PDFs, scans in
English, Ukrainian or Russian.

Free for one person for their own use, including at work; businesses need a
commercial license ([License](#license)). Linux, and macOS on Apple silicon.

## Quick start

**1. Install.** On Linux (x86_64 or aarch64, glibc 2.35 or later) or macOS 14
or later on Apple silicon, with about 3 GB of free disk:

```bash
curl -fsSL https://github.com/rundax/lifecontext/releases/latest/download/install.sh | sh
```

It installs into your home directory, asks which model to use (there is no
default: pick Anthropic, OpenAI, Google Gemini, OpenRouter, a local model
server or another OpenAI-compatible endpoint, and have its API key ready),
checks that the model answers, starts LifeContext as a user service and gives
your agents the skill. [docs/install.md](docs/install.md) explains each step.

**2. Open the vault in Obsidian.** Choose "Open folder as vault" and pick
`~/LifeContext/vault` (or the folder you gave the installer). Any Markdown
editor works too.

**3. Import a document and ask your agent.**

```bash
lifecontext documents import ~/Documents/lease.pdf
```

(If the shell does not find `lifecontext`, add `~/.local/bin` to your `PATH`
as the installer says, or open a new terminal.) Indexing and compiling call your model and can take a few minutes; the page
then appears under `Journal/wiki/` in the vault. Ask Claude Code, Codex or
Gemini CLI: *"What does my lease say about the deposit?"* The agent answers
from LifeContext's evidence: the quote, the page and the version of the lease
it came from.

## What you get

- **A wiki page for each document**, in your vault, with numbered claims. Each claim
  is a literal quote, checked against the extracted text before the page is
  written, and cited to the source, its version and the passage or page. A
  short summary above the claims is labelled as written by the model.
- **Originals kept** byte for byte, checksummed and versioned. A correction
  adds a version, and pages built on a corrected or forgotten source are
  marked stale.
- **Scans read locally**: Tesseract OCR for English, Ukrainian and Russian.
  Pages it could not read are listed, not skipped.
- **One memory for your agents**: one CLI and one skill for Claude Code, Codex,
  Gemini CLI and Hermes, and a read-only MCP server for Claude Desktop and
  Cursor.
- **Notes too**: journal entries and notes from `Journal/Inbox` are indexed and
  searchable next to your documents; compiling the journal into wiki pages
  comes in v1.3.

## Status

This tree is the 1.2.0 development version (1.2.0.dev0). 1.2.0, the first
public release, is planned for 2026-11-01; until then the install command
above and the Docker image are not available, and you can install from a
checkout ([docs/install.md](docs/install.md#installsh)).

- Each release candidate is installed on clean GitHub-hosted machines and put
  through one scenario against a real model: on Ubuntu 24.04 (x86_64) with
  `install.sh` and with Docker (install, service, `doctor`, journal entries,
  document import with OCR, recall and the wiki), and on macOS 14 (Apple
  silicon) with `install.sh` (install, the launchd service, `doctor`, one
  import and one recall). `install.sh` also runs on Linux aarch64, which is
  not qualified.
- CI runs the test suite on Ubuntu 24.04 with Python 3.12 only.
- Local models (Ollama, vLLM, LM Studio) are supported but not yet part of
  release qualification.
- Windows is not supported natively; use Docker or WSL.

## Other ways to install

### Docker

The image runs as an unprivileged user, publishes no port, and has Tesseract
and the local models inside. From a checkout:

```bash
mkdir -p secrets && (umask 077 && cat > secrets/lifecontext_api_key)   # paste the key, Ctrl-D
LIFECONTEXT_PROVIDER=anthropic LIFECONTEXT_MODEL=claude-sonnet-5 docker compose up -d
docker compose exec lifecontext lifecontext doctor
```

Set `LIFECONTEXT_PROVIDER` and `LIFECONTEXT_MODEL` to the provider and model
you chose; without them the container waits and logs which one is missing.
Agents on the host use `bin/lifecontext-docker`; see
[docs/install.md](docs/install.md#docker).

### From source

You need Python 3.12, Git, and about 3 GB of disk for the environment and
models. Tesseract OCR is optional but needed to read scans and images.

```bash
git clone https://github.com/rundax/lifecontext.git
cd lifecontext
python3.12 -m venv .venv
# constraints-cpu.txt pins CPU-only PyTorch on Linux: about 2.4 GB instead of
# about 6 GB with the default CUDA build. It changes nothing on macOS.
.venv/bin/pip install -c constraints-cpu.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu .
export PATH="$PWD/.venv/bin:$PATH"
lifecontext --version
```

Optional OCR with English, Russian and Ukrainian:

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-ukr       # Debian, Ubuntu
sudo dnf install tesseract tesseract-langpack-eng tesseract-langpack-rus tesseract-langpack-ukr # Fedora
sudo pacman -S --needed tesseract tesseract-data-eng tesseract-data-rus tesseract-data-ukr      # Arch Linux
brew install tesseract tesseract-lang                                                          # macOS
```

With [uv](https://github.com/astral-sh/uv) instead of pip, add
`--index-strategy unsafe-best-match`; without it uv takes packages such as
`urllib3` only from the PyTorch index, whose copies are too old, and the install
fails:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -c constraints-cpu.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match .
```

LifeContext is not published on PyPI, so never install the bare name
`lifecontext` from there; use a checkout (`.`) or a wheel from a
[GitHub release](https://github.com/rundax/lifecontext/releases).

Then set it up and start it:

```bash
lifecontext init                             # provider, model, key (hidden), vault, time zone
lifecontext preflight --model-probe          # a short check of the provider, key and model
lifecontext service install --maintenance    # run it as a user service, plus a daily 07:00 upkeep
                                             # add --capture to take in notes from Journal/Inbox every 30 s
lifecontext skills install                   # give Claude Code, Codex, Gemini CLI, Hermes the skill
lifecontext doctor                           # check everything; exit 1 only if something is broken
```

`init` creates `~/.lifeweave` (mode `0700`) with `config.yaml` and `.env` (mode
`0600`), and the vault `~/LifeContext/vault` with `Journal`, `Journal/entries`
and `Journal/Inbox` inside (mode `0700`). It never starts anything; running it
again changes nothing unless you pass `--force` or `--replace-key`.
`lifecontext preflight` without `--model-probe` checks the configuration and
that a key is stored, then starts the local memory engine and checks that it
answers; it does not check the model (starting the engine sends it one tiny
test request and only logs the result). `service install` writes a
`systemd --user` unit on Linux or a launchd agent on macOS (`--dry-run` shows
the files first); to run in a terminal instead, use `lifecontext serve`. The
first start downloads the local embedding and reranking models into
`~/.cache/huggingface` (about 1 GB; `install.sh` fetches fixed revisions of
them instead, about 0.6 GB). [docs/operations.md](docs/operations.md) covers
each command, unattended setup and backups.

In Claude Code you can also add the skill as a plugin:

```
/plugin marketplace add rundax/lifecontext
/plugin install lifecontext@lifecontext
```

The plugin carries only the skill, so install the `lifecontext` command first,
and use either the plugin or `lifecontext skills install` for Claude Code.

## Choose a model

LifeContext uses one model at a time and has no default: the installer and
`lifecontext init` ask you to pick the provider and the model, and the text of
your documents and notes is sent to it. With Anthropic, `claude-sonnet-5` works
and `claude-haiku-5-5` costs less; Claude Sonnet 5.5, Opus 5.5, Fable 5.1 and
Mythos 5.1 do not work yet. Change it later with `lifecontext llm set`; every
provider, field and rule, the API key and the optional vision model are in
[docs/operations.md](docs/operations.md#choosing-the-model).

## Daily use

The service must be running: the installer and `lifecontext service install`
start it, `lifecontext service status` shows it, and `lifecontext serve` runs
it in a terminal instead. Add `--json` for stable machine-readable output; reuse
an idempotency key only to retry the same request.

**Import a document.** PDF, DOCX, XLSX, PPTX, PNG, JPEG, TIFF, plain text and
Markdown. The original is kept byte for byte; its text is extracted, indexed
and compiled into a wiki page.

```bash
lifecontext documents import ~/Documents/lease.pdf
lifecontext documents list
lifecontext documents show SOURCE_ID         # stages and page coverage
```

**Read the wiki.** Each document has a page `source_<source id>` with numbered,
quote-backed claims; `index` lists them. The pages also appear under
`Journal/wiki/` in the vault.

```bash
lifecontext wiki list
lifecontext wiki show index
```

**Ask.** `recall` returns cited passages, each tied to one source version. It
reranks on the CPU and can take tens of seconds; run one at a time. `reflect`
writes an answer from cited evidence.

```bash
lifecontext recall --query "When is the rent due?"
lifecontext reflect --query "What does my lease say about the deposit?"
```

**Write a journal entry.** Put a Markdown note into `Journal/Inbox` in the
vault: `lifecontext capture --once` (or the service with `--capture`, every 30
seconds) turns it into a journal entry, `Journal/entries/YYYY-MM-DD -
Title.md`. Agents write entries with `lifecontext retain`; see
[docs/agents.md](docs/agents.md). Entries are indexed and searchable;
compiling the journal comes in v1.3.

`curate` corrects, re-indexes, forgets or deletes a source version;
`reconcile` checks the vault against custody and repairs managed files.
Forget withdraws a source from recall and the wiki but keeps it on your
machine: the original stays in custody, vault files already written stay (for
a document, its source note under `documents/`), and a document stays in
`documents list`. Delete removes the original, the vault files, the recall
index and the extracted text, and replaces its quoted text in the stored wiki
revisions and review records with `[erased]`, so use delete to remove a source.
[docs/operations.md](docs/operations.md) covers recovery, rebuilds, file names
and failure codes.

**Connect Claude Desktop or Cursor.** `lifecontext mcp` is an MCP server over
the same API, read-only unless you add `--allow-writes`; the configuration is
in [docs/agents.md](docs/agents.md#mcp-server).

**Agents on another machine.** `bin/lifecontext-remote` runs the same
`lifecontext` command over SSH, against a dedicated key that
`lifecontext agent-gate` limits to everyday agent commands (no setup, service,
backup or export); see
[docs/agents.md](docs/agents.md#agents-on-another-machine).

**Back up.** `lifecontext backup --stop-service` stops the service for the
backup and starts it again
([operations](docs/operations.md#backup-and-restore)).

## Several devices (advanced, optional)

LifeContext does not need sync: open the vault on the machine that runs it.
To read and write the vault on a phone or a second computer, the Self-hosted
LiveSync Obsidian plugin can sync it through a CouchDB hub that
`docker/livesync/` ships, published to your tailnet with `tailscale serve`.
It takes Docker, Tailscale and a headless LiveSync client on the server, which
must run the same LiveSync release as the plugin on every device;
[docs/sync.md](docs/sync.md) walks through it.

## Privacy

- Originals, the catalog, the index and the vault stay on your machine.
- Text you retain or import goes to the LLM you configure, for indexing, wiki
  compilation and category suggestions (an excerpt of up to 8,000 characters
  per document, plus your category list).
- Page images go only to a vision model you configure; vision is off by
  default.
- The first start downloads the local models from Hugging Face; once they are
  cached, later starts make no Hugging Face calls. When Hindsight starts, its
  libraries may fetch public reference files that contain none of your data: a
  model price list from raw.githubusercontent.com and a tokenizer table from
  openaipublic.blob.core.windows.net. LifeContext v1.2 turns both off once they
  are cached.
  <!-- verify at release -->
- No telemetry. The API listens on loopback only and requires its local token.

Details, including what agents and vault sync do with your data:
[docs/privacy.md](docs/privacy.md).

## License

LifeContext is **source-available**, not open source, under the
[Business Source License 1.1](LICENSE).

- Free when you are one person running your own copy for your own use,
  including at work as an employee, contractor, freelancer or sole proprietor,
  and with documents that belong to your employer or clients.
- Free for non-profit organizations, accredited educational institutions, and
  non-profit or publicly funded research institutions, for their own purposes.
- An organization that installs, provides or administers LifeContext for its
  staff or other people, and anyone offering it as a hosted, managed or
  embedded service, needs a [commercial license](COMMERCIAL-LICENSE.md).
- Each version becomes Apache-2.0 on its Change Date; for 1.2.0 that is on or
  before 2030-11-08.

The exact grant and common cases: [LICENSING.md](LICENSING.md). Third-party
components and models: [NOTICE](NOTICE). LifeContext is built on the
open-source [Hindsight](https://github.com/vectorize-io/hindsight) memory
engine (MIT, by Vectorize).

## Documentation

- [docs/install.md](docs/install.md): `install.sh` (options, unattended
  install, upgrade, uninstall), Docker and Compose, verifying a release
- [docs/operations.md](docs/operations.md): choosing the model, configuration,
  startup checks, recovery, documents, OCR, vision, wiki rebuilds, file names
- [docs/agents.md](docs/agents.md): the agent skill, how agents use the CLI,
  the MCP server for Claude Desktop and Cursor, and agents on another machine
- [docs/sync.md](docs/sync.md) (advanced, optional): Obsidian on several
  devices with Self-hosted LiveSync, CouchDB and Tailscale, and
  `lifecontext capture`
- [docs/architecture.md](docs/architecture.md): how data flows and where it is
  stored
- [docs/privacy.md](docs/privacy.md): what leaves the machine
- [docs/development.md](docs/development.md): development checkout, tests, CI,
  and the [compatibility identifiers](docs/development.md#compatibility-identifiers)
  that keep their legacy `lifeweave` names
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md),
  [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [CHANGELOG.md](CHANGELOG.md)
