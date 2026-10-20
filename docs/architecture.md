# Architecture

LifeContext is one local service (`lifecontext serve`) with a thin CLI in front
of it. Apart from `serve` itself and `preflight`, which starts and checks the
memory engine, every CLI command is an HTTP request to the loopback API. Agents
use the same CLI ([agents.md](agents.md)).

## Data flow

```text
original bytes ──► custody ──► extraction / OCR / opt-in vision ──► Hindsight index
 (journal text,     (immutable    (text layer, Tesseract, labelled     (retain with a
  PDF, DOCX, ...)    archive)      model reading; derived artifact)     per-version tag)
                                                                            │
                         Obsidian vault ◄── projection ◄── wiki compile ◄──┘
                         (Markdown you read and edit)      (claims validated
                                                            as literal quotes)
```

1. **Custody.** `retain` (journal text) and `documents import` (files) first
   store the exact original bytes, with their SHA-256, in an immutable archive
   under `~/.lifeweave/archive/`. Every later step refers to a source ID and
   version; a correction is a new version, never an edit in place.
2. **Extraction.** Documents are parsed in a separate, resource-limited worker
   process: text layers of PDF, DOCX, XLSX and PPTX, plain text and Markdown,
   and local Tesseract OCR (English, Russian, Ukrainian) for scans and images
   (PNG, JPEG, TIFF). No macros, links or embedded objects execute. A page that
   cannot be read is recorded in a coverage ledger and the rest of the document
   is kept; a PDF without any readable text still fails. With a configured
   `vision` block, only the routed pages, figures
   and pictures go to that model, and its readings are labelled as such. The
   result is an immutable extraction artifact under `~/.lifeweave/derived/`.
3. **Indexing.** Text is retained into Hindsight (memory engine, embedded
   PostgreSQL) in the `lifeweave` profile and bank. Each retain carries the tag
   `src:<source_id>:v<version>`, so `recall` can tie every result to exactly one
   local source version; results that cannot be attributed are omitted and
   counted.
4. **Wiki compilation.** For each document, the configured LLM proposes claims
   from bounded evidence windows. A claim is published only if its citation is
   a literal quote found in the cited extraction; each document gets its own
   page `source_<id>` with a labelled model summary, one routing request then
   places those checked claims on shared topic and entity pages (links both
   ways, contradictions across documents opened as reviews), and the `index`
   page is built without a model.
   Revisions are immutable artifacts under `~/.lifeweave/wiki_revisions/`.
   Citation validation proves provenance, not that the claim is a correct
   reading of the quote.
5. **Projection.** Journal entries, document notes (with a copy of the original
   as an attachment) and wiki pages are written as Markdown into the vault,
   below its journal path (by default `~/LifeContext/vault/Journal`; see
   [operations.md](operations.md#vault-time-zone-and-limits)). Generated sections sit between managed markers;
   user sections are never overwritten. A hand-edited managed file becomes a
   durable conflict instead of being replaced. `reconcile` re-checks the files
   against custody and recreates missing projections.

A document's progress is visible per stage in `documents show`: `custody`,
`extraction`, `indexing`, `wiki`, `projection`.

## Durable operations

Mutating commands accept an idempotency key (`--idempotency-key`). Retains,
corrections, document imports and retries, and wiki rebuilds are recorded in the
catalog as operations with an `op_...` ID; repeating the same key with the same
input replays the recorded result instead of submitting again. A failed step
leaves a failed operation that `operations get` shows and that can be retried
explicitly. On start, the service resumes unfinished imports, indexing and
vision reads before it answers requests; outcomes it cannot prove stay
unresolved rather than being resubmitted blindly.

## What is persisted where

| Location | Contents | Rebuildable? |
|---|---|---|
| `~/.lifeweave/config.yaml`, `.env` | Settings and the provider key (`0600`) | No (yours) |
| `~/.lifeweave/api.token` | Local API capability token | Yes, regenerated |
| `~/.lifeweave/hindsight-api.token` | Token the Hindsight daemon requires (`0600`) | Yes, regenerated on start |
| `~/.lifeweave/postgres/` | The embedded PostgreSQL's Unix socket (`0700`) | Yes, on start |
| `~/.lifeweave/archive/` | Original bytes of every source version | No: this is custody |
| `~/.lifeweave/catalog.sqlite3` | Catalog: sources, versions, stages, operations, projections, wiki revisions, categories | No |
| `~/.lifeweave/derived/` | Extraction artifacts | From the archive, by re-running extraction |
| `~/.lifeweave/wiki_revisions/` | Immutable wiki revision artifacts | By `wiki rebuild` (new LLM calls) |
| `~/.lifeweave/vision-cache/` | Cached model readings per source (vision only) | By re-reading (new model calls) |
| `~/.lifeweave/cache/tiktoken/`, `hindsight-start.json` | Hindsight's tokenizer file; how the daemon was last started (offline or not) | Yes, downloaded or written again |
| `~/.hindsight/profiles/` | Generated `lifeweave` profile environment and logs | Yes, on start |
| `~/.pg0/` | Hindsight's embedded PostgreSQL: binaries and the `lifeweave` instance data | Index data: by re-indexing from custody (LLM calls) |
| `~/.cache/huggingface/` | Downloaded embedding and reranking models | Yes, downloaded again |
| The configured vault | Markdown projections and attachments, plus your own notes | Managed files: by `reconcile` |

A backup that should survive a disk loss needs at least `~/.lifeweave` (with
`.env` kept separately and safely) and the vault. `lifecontext backup` archives
both plus the `lifeweave` Postgres instance, with a checksummed manifest; see
[operations.md](operations.md#backup-and-restore).

## Process and network boundaries

- The API binds to a loopback address only (`127.0.0.1`, default port 8765),
  requires a loopback `Host` header and rejects any request that carries an
  `Origin` header, which keeps web pages in a browser out. Every `/v1` route
  also requires the bearer token from `~/.lifeweave/api.token`, which the CLI
  reads from the same HOME; only the liveness check and the API schema are
  public.
- Hindsight runs as a separate local daemon bound to `127.0.0.1`, on a port
  chosen for this account and HOME. Every request except its health, version
  and metrics routes needs the bearer token from `~/.lifeweave/hindsight-api.token`.
  LifeContext sends the token only to a daemon it has checked is this HOME's.
- Hindsight's PostgreSQL listens on no TCP port at all, only on a Unix socket
  in `~/.lifeweave/postgres` (mode `0700`). LifeContext starts and stops it.
- What this protects against, and what not: [privacy.md](privacy.md#other-users-of-this-computer).
- Outbound connections are listed in [privacy.md](privacy.md).
