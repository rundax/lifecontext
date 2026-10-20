# Unreleased

- Security: other accounts on the same computer can no longer read or change
  your memory. The `lifeweave` Hindsight daemon used to answer anyone on
  `127.0.0.1:9855`, the same port in every HOME, and its PostgreSQL listened on
  `127.0.0.1` with the password `hindsight`, which also allowed running
  commands as you. Now the daemon requires a token from
  `~/.lifeweave/hindsight-api.token` (`0600`) on a port chosen per account and
  HOME, LifeContext never reuses, stops or sends the token to another account's
  or HOME's daemon, and PostgreSQL listens only on a Unix socket in
  `~/.lifeweave/postgres` (`0700`), started by LifeContext with pg0's binaries;
  new installs get a random database password. Existing installs move over on
  their next start (`serve` or `preflight`) without changing their data.
  `doctor` reports engine auth, who holds the daemon's port and where Postgres
  listens (`engine_auth_off`, `engine_port_taken`, `postgres_tcp`).
  `hindsight stop` also stops the profile's Postgres. Backups leave the engine
  token and the socket directory out and `restore` refuses them; the database
  password still travels in `instance.json`.
  `scripts/check_observation_scopes.py` sends the token.

- `restore` keeps every write inside the new HOME or `--vault`: without
  `--vault`, the vault location from the archive must resolve to a folder below
  `~/LifeContext` (`vault_outside_home`, `vault_location_refused`); member
  names with `..` or an absolute path, links, devices, and `.env` and
  `api.token` in any case are refused; a malformed manifest, `config.yaml` or
  `instance.json` gives a stable error code; the restored roots and vault
  folders are mode `0700` and vault files `0600`. Backups record HOME by its
  real path, so a HOME reached through a link restores. An existing empty vault
  directory (a volume mount point) or a vault on another filesystem is filled
  entry by entry and emptied again on failure. Archives remain trusted input.
- `init --force` applies the given flags to the existing `config.yaml` and keeps
  every other setting (it used to rewrite it from defaults and drop the vault,
  `vision`, `limits` and relationship roots); it no longer touches `.env`. The
  new `--replace-key` replaces only the stored key. A re-run warns
  `flags_ignored` for flags it did not apply. `init` reads
  `LIFECONTEXT_PROVIDER`, `LIFECONTEXT_MODEL`, `LIFECONTEXT_BASE_URL`,
  `LIFECONTEXT_VAULT` and the key file `LIFECONTEXT_API_KEY_FILE`, creates
  `Journal/entries`, and leaves no config behind when the vault check fails.
- The first Journal entry in a vault without `Journal/entries` no longer fails
  with `vault_unavailable`; the folder is created when the entry is written.
- `backup --stop-service` stops the user service and the `lifeweave` Hindsight
  profile, backs up and starts the service again, also after a failed stop or
  an interrupt; `lifecontext hindsight stop`
  stops only that profile and its Postgres. `backup` copies the archive into
  place where the destination has no hard links.
- `service install --no-start` writes and enables the units without starting
  them; a launchd re-install loads an agent whose first load failed; `service
  uninstall` removes the files even without a user bus and lists what failed
  under `warnings`. The API unit gets `MemoryHigh=4G` and `Nice=5`, a literal
  `$` in `Environment=` is no longer doubled, and the units go to the account's
  home with `Environment=HOME=` when the data lives under another HOME.
- `uv pip` installs need `--index-strategy unsafe-best-match` with the PyTorch
  CPU index; the docs say so and CI resolves that install against the wheel.
  The examples install a release wheel or a checkout, never the bare name from
  PyPI.

- Add `install.sh`: installs a pinned uv, Python 3.12 and LifeContext with
  CPU-only PyTorch into `~/.local/share/lifecontext` for the current user
  (Linux x86_64/aarch64 with glibc 2.35 or later, macOS 14 or later on arm64),
  verifies release checksums (and the cosign signature with cosign 3 or
  later), installs every dependency from a hashed per-platform lock
  (`requirements/`), fetches the local models at fixed revisions, installs
  Tesseract with eng/rus/ukr when that works without a password, then runs
  `init`, `service install --maintenance`, `skills install` and `doctor`. A
  re-run upgrades in place: it stops the service, backs up, installs and
  starts the service again, and keeps settings, key and data. `--assets DIR`
  installs from downloaded release assets; `--uninstall` removes only what it
  installed. See `docs/install.md`.
- Add `.github/workflows/release.yml` for `v*` tags: builds the assets without
  write access, signs `SHA256SUMS` and the multi-arch GHCR image keylessly,
  creates a draft release and publishes it only for a final version after the
  image is pushed and signed.
- Add a Docker image (`Dockerfile`), `compose.yaml` and the
  `bin/lifecontext-docker` wrapper: non-root, no published port, the API on
  loopback inside the container, Tesseract and the local models in the image
  (it starts with `HF_HUB_OFFLINE=1`), setup from `LIFECONTEXT_*` variables and
  a key file on the first start, named volumes for `/data/home` and
  `/data/vault`. The wrapper runs commands with `docker exec` and passes host
  files in and out, so the agent skill works unchanged.
- Add `.github/workflows/qualify.yml`, a manual workflow that installs with
  `install.sh` and with Docker on clean runners and runs a synthetic scenario
  against a CI model, keeping the evidence.
- Add `constraints-cpu.txt`, which pins CPU-only PyTorch on Linux for pip and
  `uv pip` with the PyTorch CPU index; the README, CI, `install.sh` and the
  Docker image install with it.
- Make the repository a Claude Code plugin marketplace:
  `/plugin marketplace add rundax/lifecontext`, then
  `/plugin install lifecontext@lifecontext` adds the journal skill.
- `init` now names `service install`, `skills install` and `doctor` in its next
  steps.

- Add `lifecontext init`: guided, idempotent setup of `~/.lifeweave`
  (`config.yaml`, `.env`), the vault and its `Journal`, and the time zone, with
  private modes. The key comes from a hidden prompt or `--api-key-stdin`, never
  from the command line. Unattended flags: `--provider --model --base-url
  --vault --timezone --non-interactive --force`. Anthropic `claude-sonnet-5` is
  the suggested model, `claude-haiku-5-5` the cheaper one.
- A loopback `openai_compatible` endpoint (Ollama, llama.cpp, vLLM) no longer
  needs `LIFEWEAVE_LLM_API_KEY`.
- Add `lifecontext doctor [--json]`: read-only diagnostics (versions, paths and
  modes, config summary without secrets, service readiness and lint counts,
  operation counts, Tesseract languages, vision, model cache and offline start,
  disk, `command -v lifecontext`). It exits 1 only for broken prerequisites and
  prints no private content.
- Add `lifecontext service install|uninstall|status [--maintenance]
  [--dry-run]`: a `systemd --user` unit `lifecontext.service` (or a launchd agent
  on macOS) that runs the installed `lifecontext serve` and restarts it on
  failure, and with `--maintenance` a daily 07:00 timer with memory, CPU and
  priority limits. Only its own unit files are ever written or removed.
- Add `lifecontext maintain [--date D] [--retry-wiki N] [--rebuild-stale N]
  --json`: reconcile, wiki lint and the briefs for yesterday and today, without
  model calls unless a rebuild option is given; the last run is recorded for
  `service status`.
- Add `lifecontext backup [DEST]` and `lifecontext restore ARCHIVE [--vault
  DIR]`. A backup refuses to run while the service or its Postgres runs, holds
  the service lock while it reads, and writes one `tar.gz` (mode `0600`, with a
  `.sha256` file) of `~/.lifeweave` without `.env`, the token, locks and caches,
  the Hindsight profile's embedded Postgres instance and the vault, with a
  manifest of every entry's mode, size and SHA-256. A restore goes only into a
  HOME without LifeContext data: it verifies the whole archive first, extracts
  into a staging directory, checks the catalog and its schema version, rewrites
  the paths for the new HOME and moves each tree into place.
- Ship the agent skill in the wheel and add `lifecontext skills
  install|check|remove [--agent claude|codex|gemini|hermes|all] [--dry-run]`,
  which links Claude Code, Codex, Gemini CLI and Hermes to the installed copy,
  backs up a conflicting entry first and removes only its own links. The skill
  moved to `src/lifecontext/agent_skill/`; `skills/personal-journal-operations`
  is a relative link to it, and `scripts/install_agent_skill.sh` now calls the
  command.
- Start Hindsight with `HF_HUB_OFFLINE=1` once the embedding and reranking
  models are in the Hugging Face cache, so restarts make no Hub calls. Always
  set `LITELLM_LOCAL_MODEL_COST_MAP=True` (no price-list fetch from GitHub on
  every start) and keep the tiktoken file in `~/.lifeweave/cache/tiktoken`.

- Renamed to LifeContext: distribution, command and Python package are now
  `lifecontext`; `life-context` remains a command alias; stored identifiers are
  unchanged.

- Add `lifecontext mcp`, a stdio MCP server for Claude Desktop, Cursor and other
  MCP clients (official `mcp` SDK 2.2.0). It is a thin client of the local API
  with the CLI's token, limits, timeouts and error codes. Read-only by default:
  `status`, `recall`, `reflect`, `evidence`, `wiki_list`, `wiki_show`,
  `wiki_lint`, `wiki_reviews`, `brief`, `documents_list`, `document_show`,
  `operation_show`. `--allow-writes` adds `retain` and `wiki_reviews_resolve`,
  and with `--import-dir DIR` also `documents_import`, each with a required
  idempotency key. Imports read only regular files inside those directories,
  never the LifeContext home, the vault, hidden directories in the home
  directory or system directories. No forget, delete, curation or category
  tools. `recall`/`reflect` and imports run one at a time (`busy` otherwise)
  and send progress notifications while they wait. Setup and client timeouts
  in `docs/agents.md`.

- Give every document its own wiki page. A large book no longer overflows the
  shared `index` page: each document compiles into `source_<id>`, holding at
  most 48 claims sampled evenly across the whole document (8 per document on
  shared topic pages), and recompiling replaces its claims instead of adding to
  them. A page that fills up stops asking the model instead of failing. `index`
  becomes a model-free table of contents linking the document pages. Run
  `wiki rebuild` once per document to move existing wikis to this layout.
- A forgotten document no longer shows anywhere in the wiki. Its contradiction
  reviews leave `wiki reviews`, the brief and `wiki lint`; pages that cite only
  forgotten or deleted documents are titled "Withdrawn page" in `wiki list`,
  `wiki status` and `wiki show`, have no title in new briefs and logs, and
  their vault files and history files are renamed to match. A forget or delete
  rewrites those vault files at once instead of at the next `reconcile`, and
  each document version is checked on its own. A forgotten document takes no
  new version (`invalid_request`) and is never re-indexed (`conflict`), so
  recall cannot cite it again; `curate correct` of a forgotten journal entry
  now fails with `conflict` too. `documents show` of a deleted document
  returns `source_not_found`. A delete also rewrites the briefs that name or
  quote the document, and the log, and deleted documents leave `documents
  list`. The
  README and `curate --help` say what forget keeps and delete removes. A full
  shared page now leaves out the compiling document's claims first, so other
  documents keep theirs.

- Account for every page of every document. One OCR timeout, failed or
  oversized page, or empty TIFF frame no longer fails a whole document: it is
  flagged (`ocr_failed_page:N; <reason>`) and the rest is kept. A mixed PDF
  without Tesseract keeps its text pages (`ocr_unavailable_page:N`). Blank pages
  are recognised before OCR (`blank_page:N`). Pictures and drawings on pages
  that have text are now detected from PDF object data (`figure_not_read:N`),
  and garbled text layers are flagged (`text_layer_suspect:N`).
- `documents show` and `documents list` report `coverage`: how many pages or
  frames were read from the text layer, by OCR or by a model, how many are
  blank, and which are unresolved or only partly read. The source note in the
  vault starts with the same summary and a "Not read" list, and shows its text
  under page, slide, sheet, header and footnote headings instead of raw locators.
- Read more of Office files: DOCX headers, footers, footnotes, endnotes and text
  boxes; PPTX speaker notes, SmartArt text and chart data (as Markdown tables).
  Pictures in DOCX, PPTX and XLSX and charts in XLSX enter the ledger
  (`embedded_image_not_read`, `chart_not_read`).
- Add an optional `vision` block (`openai_compatible`, `openai`, `anthropic`,
  `gemini`) that reads handwriting, photos, diagrams, charts and image tables.
  Off unless configured, with its own `LIFEWEAVE_VISION_API_KEY`. Only routed
  items are sent; every model reading is labelled and indexed in its own chunks;
  readings are cached per source and deleted with it; an endpoint outage fails
  the import retryably and a plain retry resumes from the cache. `documents
  import --no-vision` opts one document out for good; `preflight --vision-probe`
  checks that the model can see. Every extraction revision made after this
  change has a new `config_identity`.
- The CLI waits up to 31 more minutes for document imports and retries, to
  cover vision reading and its capability probe.
- Very long extraction ledgers are compacted into page lists and ranges so a
  large illustrated document stays within the catalog's 1000-entry bound.

- Name every journal entry `YYYY-MM-DD - Title`. The date now also comes from
  a date line among the entry's opening lines
  (`Entry date: 2026-10-12 (Europe/Vienna).`, `Mon, Sep 14, 2026`,
  `13 жовтня 2026 р.`) and otherwise from the day the source was retained, in
  the new optional top-level `timezone` setting. Lines that only carry the
  date, timezone or provenance are no longer used as titles; the title is then
  the opening sentence. `reconcile` renames unchanged entries that still carry
  an undated name; edited entries are left untouched. Sort Obsidian's file list
  by "File name (Z to A)" to see the newest entries first.

- Give every local agent one copy of the journal skill. `scripts/install_agent_skill.sh`
  links the repository skill into Hermes, Claude Code, Codex and the shared agent
  directory (`--check` reports drift). The skill (1.5.0) now states operator
  boundaries: agents use only the CLI, report failures with the exact command and
  output instead of repairing code, config, services or data, and run `recall`
  one at a time with a long timeout.

- Make `recall` reliable. Every retain now carries a per-source-version tag so
  Hindsight consolidates observations within one source and never merges facts
  from different sources into an observation recall cannot attribute. A result
  that still cannot be tied to exactly one local source version is omitted and
  counted in `omitted_unresolvable` instead of failing the whole query; a query
  with nothing citable still fails. Re-run `curate reindex` for journal sources
  indexed before this change.
- Resolve recall items that Hindsight returns without version metadata through
  their memory record, with the same custody checks, instead of rejecting them.
- Add `scripts/check_observation_scopes.py` to detect (and with `--apply`
  repair) Hindsight observations that merge several sources.
- Bound local reranking cost: rerank the top 100 fused candidates, batch by
  length and allow two concurrent rerank jobs. Restart the `lifeweave` dev profile
  to apply.
- Rerank with the multilingual `nreimers/mmarco-mMiniLMv2-L6-H384-v1` instead of
  the English-only default. Journal and document text is Ukrainian, Russian and
  English; the English vocabulary splits Cyrillic into characters, which made
  recall slower and a Russian query return 2 of 10 matching results instead of 8.
  Reranking happens at query time, so nothing is re-indexed.
- Project wiki pages that cite a document during its projection stage, so
  imported books appear in the Obsidian wiki without waiting for `reconcile`.
- Use readable titles for LLM wiki Markdown pages and history, retaining wiki
  IDs in metadata/aliases and updating index/history links. Preserve user
  sections and stored LLM evidence; reuse durable publication/move recovery.
- Use readable date/title journal filenames while retaining stable source IDs
  and checksums in metadata. Safely migrate unchanged legacy entries during
  reconciliation, with resumable catalog moves and preserved relationship decisions.
- Upgrade the catalog to schema 8; use catalog paths for recovery, deletion,
  and verified sync handoffs. Edited or conflicting notes remain untouched.

- License LifeContext under the Business Source License 1.1 (source-available:
  free for one person running their own copy, including at work with an
  employer's or clients' documents, and for non-profit, educational and
  research institutions; a commercial license for an organization deploying it
  for others or for offering it as a service; 1.2.0 becomes Apache-2.0 on or
  before 2030-11-08), with `LICENSING.md`, `COMMERCIAL-LICENSE.md`, `NOTICE`,
  `SECURITY.md`, `CONTRIBUTING.md` and a CLA. `README.md` is now a user guide;
  `OPERATIONS.md` moved to `docs/operations.md`, development notes to
  `docs/development.md`, and `docs/` gains architecture, privacy and agent pages.

# Life Context 1.1.0 — 2026-09-30

- Add a fail-closed dev launcher usable from any working directory, preserving
  argv, relative input paths and piped stdin, with private roots and port18765.
- Automatically project immutable source versions on capture/correction;
  expose managed reconciliation through CLI/API and verify actual files,
  restoring missing output while preserving conflicting edits.
- Restore and document the local TLS-verified LLM route and dev API lifecycle.
- Retire legacy Hermes Journal tools, duplicate writer, CLI/config/scripts and
  consumer instructions while preserving generic memory support and history.
- Add concurrent external CLI acceptance and a private, exact-custody-verified
  Markdown hash handoff for separately authorized synchronization.

Verified: full V1 suite232 tests (two old opt-in suites skipped),192 focused
retirement/generic-provider/contract tests, live synthetic two-session
capture/correction/retry, stable API stop/restart, backup restore and private
handoff. Independent code/integration reviews found no blockers.

Production Life Context activation and LiveSync are not enabled by this release.
The test vault includes clearly synthetic acceptance records. Reconciliation
requires its complete inventory to fit the explicit limit (default200/max4096).
Live production before/after data hashes and functional messaging/UI smoke
checks were not collected; scope-based preservation and service status were
verified. See OPERATIONS.md for lifecycle, recovery and private handoff use.
