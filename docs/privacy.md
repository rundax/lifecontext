# Privacy: what leaves your machine

LifeContext runs on your computer. Your originals, the catalog, the index
database and the vault stay on local disk. Data leaves the machine only in the
cases below, and only to endpoints you configure or install from.

## 1. Text to the LLM provider you configure

The `llm` block in `~/.lifeweave/config.yaml` names one provider, one model and
at most one URL. LifeContext and its memory engine, Hindsight, send that
provider:

- the text of what you retain: journal entries and the text extracted from your
  documents, for fact extraction and indexing;
- facts derived from that text, when Hindsight consolidates them into
  observations;
- your `reflect` questions and the retrieved passages used to answer them
  (`recall` searches and reranks with the local models);
- bounded evidence windows from a document, when its wiki page is compiled;
- for a category suggestion on import, an excerpt of up to 8,000 characters of
  the document's extracted text plus your category list;
- one fixed synthetic test text, when you run `lifecontext preflight
  --retain-smoke`.

The request carries your provider key from `~/.lifeweave/.env`. What the
provider does with it is governed by your agreement with that provider. A
loopback endpoint (`http://127.0.0.1`, for example a local model server) keeps
this text on the machine; running fully without a remote model is not yet
qualified.

## 2. Page images, only if you turn vision on

Vision is **off** unless `config.yaml` has a `vision` block; it never inherits
the `llm` settings. When it is on, only the items the local router selects
(pages without a usable text layer, figures on text pages, embedded pictures)
are sent, as JPEG images of at most 2000 px, to the endpoint that block names.
`lifecontext preflight --vision-probe` and the service's first vision use send
one generated test image. `documents import --no-vision` keeps a document away
from vision for good.

## 3. Downloads

- Installing LifeContext downloads Python packages from PyPI (and, if you
  choose CPU-only PyTorch, from `download.pytorch.org`).
- The local models come from Hugging Face into `~/.cache/huggingface`: the
  embedding model `BAAI/bge-small-en-v1.5` (about 128 MB) and the reranker
  `nreimers/mmarco-mMiniLMv2-L6-H384-v1` (about 450 MB). `install.sh` fetches
  both at fixed revisions, about 0.6 GB, and the Docker image has them inside.
  In a from-source install Hindsight downloads them on its first start, about
  1 GB on Linux as measured in CI, since transformers also fetches a
  converted copy of the reranker. These requests contain no content of yours.
  Once both are complete in the cache, LifeContext starts Hindsight with
  `HF_HUB_OFFLINE=1`, so later starts make no Hugging Face calls;
  `lifecontext doctor` shows the cache state.
- Hindsight's token counting downloads the `cl100k_base` tokenizer file once from
  `openaipublic.blob.core.windows.net` into `~/.lifeweave/cache/tiktoken`. The
  file is public and contains none of your data.
- LifeContext sets `LITELLM_LOCAL_MODEL_COST_MAP=True` for the service and
  Hindsight, so the litellm library uses its bundled model price list instead of
  fetching one from raw.githubusercontent.com on every start.
  <!-- verify at release -->
- The embedded PostgreSQL is bundled in the `pg0` package and unpacked locally;
  it is not downloaded at run time.

## 4. What does not leave the machine

- Embedding, search, reranking, OCR (Tesseract) and document parsing run
  locally.
- The originals in the custody archive are never uploaded. Only extracted text
  (section 1) and, with vision on, selected page images (section 2) are sent.
- **No telemetry.** LifeContext sends no usage data, crash reports or analytics.
  Hindsight's optional OpenTelemetry tracing is off by default and LifeContext
  does not enable it.
- The API listens on loopback only, refuses browser requests, and requires the
  local token in `~/.lifeweave/api.token` on every `/v1` route.

## Other users of this computer

LifeContext keeps your data private from other accounts on the same machine,
including other accounts' LifeContext installs:

- **Files.** `~/.lifeweave`, `~/.hindsight`, the Postgres instance directory
  and the vault are mode `0700`; keys, tokens and `instance.json` (the Postgres
  password) are `0600`. No secret is put in a log, in `doctor` output or in a
  service unit, nor on a command line apart from the one-time start below.
- **The memory engine.** Hindsight listens on `127.0.0.1` on a port chosen for
  your account and HOME, and answers memory requests only with the token in
  `~/.lifeweave/hindsight-api.token`, renewed at each daemon start. Without it,
  it answers only its health, version, metrics and bank-template-schema routes
  (`/metrics` carries counters, not content). A web
  page aimed at that port (DNS rebinding) cannot read the token either.
  LifeContext sends the token only to a daemon it has checked is yours and this
  HOME's; it never reuses or stops another account's daemon.
- **The database.** Hindsight's PostgreSQL listens on no TCP port, only on a
  Unix socket in `~/.lifeweave/postgres` (mode `0700`), so other accounts cannot
  connect, whatever password they know.

What this does not protect against:

- **Your own account and root.** Any program running as you, and the
  administrator, can read every file above, the tokens and the database. Run
  LifeContext only under an account you trust with your journal.
- **The first start of a new install.** Hindsight's PostgreSQL comes from the
  `pg0` package, which creates a database only by starting it on `127.0.0.1`
  with a password on its command line. For those few seconds another local
  user who watches the process list could connect to the new, empty database
  as its superuser, which can also run commands as you. LifeContext stops it at
  once and from then on starts PostgreSQL only on the socket. (After a restore
  pg0 unpacks the binaries through a throwaway instance with no TCP at all.)
  Mounting `/proc` with `hidepid=2` (an administrator's choice) hides command
  lines from other users.
- **Backups.** An archive holds `instance.json` and with it the database
  password; keep archives as private as the data in them.
- **Older installs** (1.1, and 1.2.0 development builds before engine auth).
  Their daemon had no token, and their PostgreSQL listened on `127.0.0.1` with
  the default password `hindsight`. The first `serve` or `preflight` after the
  upgrade stops the old daemon, moves PostgreSQL to the socket and starts the
  daemon with a token on its own port. The database keeps its old password,
  which no longer matters: nothing can reach it over TCP. Until that restart
  the old daemon stays open; `lifecontext doctor` reports `engine_auth_off`.
  On a computer shared with untrusted users, an old install may already have
  been read or changed through that opening, which the move cannot detect.
- **Another HOME of your own account** taking the daemon's port while the
  service runs: before every request the service checks only that the port's
  listener is your account's (the full check, which tells HOMEs apart, runs at
  start).

## Things you control that also move data

- **Your vault.** LifeContext writes Markdown notes, wiki pages and a copy of
  each imported original (as an attachment) into the vault you configure. If
  you sync that vault (Obsidian Sync, LiveSync, Git, a cloud folder), those
  files go wherever your sync sends them. [sync.md](sync.md) describes a
  Self-hosted LiveSync hub that listens on the server's loopback only and is
  published to your tailnet alone.
- **Your agents.** An agent that runs the LifeContext CLI (Claude Code, Codex,
  Gemini CLI and others) or connects through `lifecontext mcp` (Claude Desktop,
  Cursor) sends what it reads, such as recall results and wiki
  pages, to its own model provider. Connected agents act with your authority;
  the skill tells them what not to do, but the application does not enforce it.
  See [agents.md](agents.md).
- **Bundles.** `lifecontext export` writes a portable bundle file; where it goes
  next is up to you.

See [architecture.md](architecture.md) for where each kind of data is stored
and [operations.md](operations.md#optional-vision-model-for-pictures-handwriting-and-charts)
for the vision settings.
