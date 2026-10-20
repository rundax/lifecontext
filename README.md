# LifeContext

**Your private, durable context layer for AI**

LifeContext keeps your journal entries and documents on your own machine,
indexes them with the Hindsight memory engine and the LLM you choose, and
compiles a wiki in your Obsidian vault where every claim is a checked quote
from a source. Any agent that can run a shell command (Claude Code, Codex,
Gemini CLI, Hermes) reads and adds to it through one CLI and one skill, with
citations back to the exact source version.

LifeContext is source-available under the Business Source License 1.1: free for
one person running their own copy, including at work, and for non-profit,
educational and research institutions (see [License](#license)).

## Status

This tree is the 1.2.0 development version (1.2.0.dev0); 1.2.0 will be the first public release.

- CI runs the test suite on Ubuntu 24.04 with Python 3.12 only. The package
  declares Python 3.11 to 3.13; 3.11 and 3.13 are not tested.
- A manual CI job installs the package on macOS 14 (arm64) and imports it.
  Nothing else is checked on macOS.
- Windows is not supported natively.

## Quick start

Pick one of three ways: the `install.sh` script (Linux x86_64 and aarch64,
macOS on Apple silicon), the Docker image, or from source.
[docs/install.md](docs/install.md) covers the script and Docker in full.

**install.sh.** Once v1.2.0 is published (the repository is not public yet),
one command installs uv, Python 3.12, LifeContext with CPU-only PyTorch and
Tesseract into your home directory, then runs `init`, `service install
--maintenance`, `skills install` and `doctor` (steps 2 and 3 below):

```bash
curl -fsSL https://github.com/rundax/lifecontext/releases/latest/download/install.sh | sh
```

Until then, run it from a checkout with a wheel built from the tree:
`sh install.sh --wheel dist/lifecontext-*.whl --lock requirements/linux-x86_64.txt`
([how](docs/install.md#installsh)). Run it again to upgrade; `--uninstall`
removes the installation and keeps your settings, key, vault and backups.

**Docker.** The image runs as an unprivileged user, publishes no port, and has
Tesseract and the local models inside. From a checkout:

```bash
docker compose build                       # until the 1.2.0 image is published
mkdir -p secrets && (umask 077 && cat > secrets/lifecontext_api_key)   # paste the key, Ctrl-D
LIFECONTEXT_MODEL=claude-sonnet-5 docker compose up -d
docker compose exec lifecontext lifecontext doctor
```

Agents on the host use `bin/lifecontext-docker`; see
[docs/install.md](docs/install.md#docker).

**From source:**

### 1. Install

You need Python 3.12 (see [Status](#status)), Git, and about 3 GB of disk for
the environment and models. Tesseract OCR is optional but needed to read scans
and images.

```bash
git clone https://github.com/rundax/lifecontext.git
cd lifecontext
python3 -m venv .venv
# constraints-cpu.txt pins CPU-only PyTorch on Linux: about 2.4 GB instead of
# about 6 GB with the default CUDA build. It changes nothing on macOS.
.venv/bin/pip install -c constraints-cpu.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu .
export PATH="$PWD/.venv/bin:$PATH"
lifecontext --version

# Optional OCR (Debian/Ubuntu): English, Russian and Ukrainian
sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-ukr
```

With [uv](https://github.com/astral-sh/uv) instead of pip, add
`--index-strategy unsafe-best-match`; without it uv takes packages such as
`urllib3` only from the PyTorch index, whose copies are too old, and the install
fails:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -c constraints-cpu.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match .
```

LifeContext is not published on PyPI, so never install the bare name
`lifecontext` from there; use a checkout (`.`) or a wheel from a
[GitHub release](https://github.com/rundax/lifecontext/releases).

### 2. Configure

```bash
lifecontext init
```

`init` asks for the model provider (Anthropic by default), the model, the vault
folder and the time zone, and reads your API key with a hidden prompt. It
creates `~/.lifeweave` (mode `0700`) with `config.yaml` and `.env` (mode
`0600`), and the vault `~/LifeContext/vault` with `Journal` inside (mode
`0700`). Running it again changes nothing; `--force` applies the flags you give
to `config.yaml` and keeps your other settings, and `--replace-key` replaces
the stored key and nothing else. It never starts anything.

The suggested Claude model is `claude-sonnet-5`; `claude-haiku-5-5` costs less.
Claude Sonnet 5.5 and Opus 5.5 do not work yet: they reject the forced tool
call that the pinned Hindsight uses to extract facts.

For an unattended install, pass everything as flags and the key on stdin:

```bash
lifecontext init --non-interactive --api-key-stdin --model claude-sonnet-5 \
  --timezone Europe/Vienna < ~/anthropic-key.txt
```

`--provider`, `--base-url` and `--vault DIR` select another provider or vault;
`LIFECONTEXT_PROVIDER`, `LIFECONTEXT_MODEL`, `LIFECONTEXT_BASE_URL`,
`LIFECONTEXT_VAULT` and `LIFECONTEXT_API_KEY_FILE` (a file holding the key) do
the same from the environment, and flags win.
[Configure the model](#configure-the-model) lists every provider and rule, and
the files `init` writes can also be edited by hand.

### 3. Start

```bash
lifecontext preflight                        # checks config and key, starts the local memory engine
lifecontext service install --maintenance    # run it as a user service, plus a daily 07:00 upkeep
lifecontext skills install                   # give Claude Code, Codex, Gemini CLI, Hermes the skill
lifecontext doctor                           # check everything; exit 1 only if something is broken
```

`service install` writes a `systemd --user` unit on Linux or a launchd agent on
macOS (`--dry-run` shows the files first). To run in a terminal instead, use
`lifecontext serve` (loopback only, port 8765) and `lifecontext status --json`
from a second terminal.

The first start downloads the local embedding and reranking models into
`~/.cache/huggingface` (about 1 GB on Linux, measured in CI; install.sh fetches
fixed revisions of them instead, about 0.6 GB); once they are there, later
starts make no Hugging Face calls. Open the vault folder in Obsidian to read what LifeContext
writes. Back it all up with `lifecontext backup --stop-service`, which stops
the service for the backup and starts it again
([operations](docs/operations.md#backup-and-restore)).

In Claude Code you can also add the skill as a plugin:

```
/plugin marketplace add rundax/lifecontext
/plugin install lifecontext@lifecontext
```

The plugin carries only the skill, so install the `lifecontext` command first,
and use either the plugin or `lifecontext skills install` for Claude Code.

## Configure the model

LifeContext reads exactly one LLM configuration from
`~/.lifeweave/config.yaml`. Set these fields:

```yaml
llm:
  provider: openai_compatible
  model: "<exact model ID from your provider>"
  base_url: "https://<provider API host>/v1"
  # reasoning_effort: high
```

This example selects the **OpenAI-compatible protocol**, not a specific vendor.
Use it for a service that implements the OpenAI Chat Completions API. Set
`base_url` to that service's API root, usually ending in `/v1`; do not include
`/chat/completions`. LifeContext maps this protocol to Hindsight's OpenAI
adapter.

You can instead set `provider` to one of these exact native adapter IDs:

`openai`, `openai-responses`, `anthropic`, `gemini`, `groq`, `openrouter`,
`minimax`, `ollama-cloud`, `deepseek`, `volcano`, `requesty`, `zai`,
`opencode-go`, `atlas`, or `fireworks`.

For a native provider, `model` is that provider's exact model ID. `base_url`
may be omitted to use the adapter's built-in endpoint, or set to a custom API
root when the provider adapter supports it. It is required for `volcano`,
which has no built-in endpoint in the pinned adapter. The native Gemini adapter
does not support a custom URL, so omit `base_url` for `gemini`. Choose
`openai_compatible` when a different service exposes the OpenAI Chat
Completions protocol.

LifeContext supports one selected provider, one model, at most one custom URL,
and one key at a time. There is no provider fallback or provider-specific key
lookup. Unknown provider IDs and unknown `llm` fields are rejected. Do not add
credentials to YAML.

### Reasoning effort

`llm.reasoning_effort` is optional. LifeContext accepts `low`, `medium`,
`high`, or `xhigh`; omit it to leave the model's effort at the provider
default. When set, it configures Hindsight's global LLM reasoning effort for
its operations. The selected model and endpoint must support the requested
value. The pinned native Anthropic and Gemini adapters do not apply this
setting, so LifeContext rejects it for those providers. This controls request
effort; it does not expose private chain-of-thought text. Hindsight may omit
the setting for model IDs it recognizes as incompatible, and a custom endpoint
may reject unsupported values.

LifeContext sets Hindsight's fact-extraction output limit to 8,192 tokens.
The selected model endpoint must accept that output limit; this setting is
fixed internally and is not a separate user config field.

### API key

Put the selected provider's API key in the private file
`~/.lifeweave/.env`, under this exact name:

```dotenv
LIFEWEAVE_LLM_API_KEY=<your provider API key>
```

The key is required for every provider except `openai_compatible` with a
loopback `base_url` (a local Ollama, llama.cpp or vLLM server), which needs
none. Do not use `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `HINDSIGHT_LLM_API_KEY`,
or an ambient shell variable. LifeContext reads only `LIFEWEAVE_LLM_API_KEY`
(and, for vision, `LIFEWEAVE_VISION_API_KEY`) from its own `.env` file. It
passes the LLM key to the isolated Hindsight profile at startup; Hindsight's
generated profile file is managed internally and must not be edited by hand.

### URL rules

- Remote endpoints must use `https://`.
- `http://` is allowed only for loopback hosts (`localhost`, `127.0.0.0/8`,
  or `::1`) for local development.
- Do not put a username, password, query, or fragment in `base_url`.
- Do not put an API key in the URL.

### Optional: read pictures, handwriting and charts

Typed text comes from each page's text layer, and scans are read by local
Tesseract OCR. Everything else (handwriting, photos, diagrams, charts, tables
stored as images) is listed by `documents show` under `coverage.unresolved` or
`coverage.partial` until you choose a vision model:

```yaml
vision:
  provider: anthropic            # openai_compatible | openai | anthropic | gemini
  model: "<exact vision model ID>"
  # base_url: "http://127.0.0.1:11434/v1"   # required for openai_compatible
  # max_pages_per_document: 200
```

Vision is off without this block and never inherits the `llm` settings. When it
is on, only the pages that need it are sent, as images, to the endpoint you
name. Its key is `LIFEWEAVE_VISION_API_KEY` in the same `.env`; a local
OpenAI-compatible server (Ollama, llama.cpp, vLLM) needs none. Model-read text
is always labelled so you can tell it from the original. `lifecontext
preflight --vision-probe` checks that the model can see images, and
`documents import --no-vision` keeps a single document away from it. See
[docs/operations.md](docs/operations.md) for the details.

## Daily use

The service must be running (`lifecontext serve`). Add `--json` for stable
machine-readable output; reuse an idempotency key only to retry the same
request.

**Capture a journal entry.** Each entry is a source with your own ID and a
version number; it becomes `Journal/entries/YYYY-MM-DD - Title.md` in the
vault.

```bash
printf '%s\n' 'Entry date: 2026-10-17. Walked to the lake and decided to start the Spanish course in November.' |
  lifecontext retain journal-2026-10-17-lake 1 --idempotency-key journal-2026-10-17-lake --json
# Read it back, including the stored text
lifecontext evidence journal-2026-10-17-lake --version 1 --include-content --json
```

**Import a document.** PDF, DOCX, XLSX, PPTX, PNG, JPEG, TIFF, plain text and
Markdown. The original is kept byte for byte; its text is extracted, indexed
and compiled into a wiki page.

```bash
lifecontext documents import ~/Documents/lease.pdf --json
lifecontext documents list --json
lifecontext documents show SOURCE_ID --json   # stages and page coverage
```

**Read the wiki.** Each document has a page `source_<source id>` with numbered,
quote-backed claims; `index` lists them. The pages also appear under
`Journal/wiki/` in the vault.

```bash
lifecontext wiki list --json
lifecontext wiki show index --json
```

**Ask.** `recall` returns cited passages, each tied to one source version. It
reranks on the CPU and can take tens of seconds; run one at a time. `reflect`
writes an answer from cited evidence.

```bash
lifecontext recall --query "When did I decide to start the Spanish course?" --json
lifecontext reflect --query "What did I decide about learning Spanish?" --json
```

`curate` corrects, re-indexes, forgets or deletes a source version;
`reconcile` checks the vault against custody and repairs managed files.
Forget withdraws a source from recall and the wiki but keeps it on your
machine: the original stays in custody, vault files already written stay (for
a document, its source note under `documents/`), and a document stays in
`documents list`. Delete removes the original, the vault files and the recall
index, so use delete to remove a source.
[docs/operations.md](docs/operations.md) covers recovery, rebuilds, file names
and failure codes.

**Connect Claude Desktop or Cursor.** `lifecontext mcp` is an MCP server over
the same API, read-only unless you add `--allow-writes`; the configuration is
in [docs/agents.md](docs/agents.md#mcp-server).

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
components and models: [NOTICE](NOTICE).

## Documentation

- [docs/install.md](docs/install.md): `install.sh` (options, unattended
  install, upgrade, uninstall), Docker and Compose, verifying a release
- [docs/operations.md](docs/operations.md): configuration, startup checks,
  recovery, documents, OCR, vision, wiki rebuilds, file names
- [docs/architecture.md](docs/architecture.md): how data flows and where it is
  stored
- [docs/privacy.md](docs/privacy.md): what leaves the machine
- [docs/agents.md](docs/agents.md): the agent skill, how agents use the CLI,
  and the MCP server for Claude Desktop and Cursor
- [docs/development.md](docs/development.md): development checkout, tests, CI,
  and the [compatibility identifiers](docs/development.md#compatibility-identifiers)
  that keep their legacy `lifeweave` names
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md),
  [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [CHANGELOG.md](CHANGELOG.md)
