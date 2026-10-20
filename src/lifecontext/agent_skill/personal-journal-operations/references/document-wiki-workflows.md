# Agent-operated documents, wiki, and outputs

Use `CLI=lifecontext` from SKILL.md. `status --json` checks service
readiness, not successful LLM generation. Commands below are for the agent to
execute. The skill ships with the installed LifeContext package; agents load it
through links created by `lifecontext skills install`. Follow the operator
boundaries in SKILL.md: report failures instead of repairing the environment.

## Choose the input and preserve identity

- “Add this file to my wiki”: import its original bytes, not a summary.
- “Add this information to my wiki”: save the complete supplied text in a
  private temporary `.txt` or `.md` file and import it. Keep agent commentary
  and recommendations separate from the original.
- Ordinary diary capture uses `retain` and its read-back reference. Do not
  submit the same information through both paths unless explicitly requested.
- Locate the specific attachment/path; do not ingest every nearby file. Ask
  only if the requested file is inaccessible or genuinely ambiguous.
- Use a unique stable idempotency key per mutation. Record it, input checksum,
  operation ID, source ID/version, and exact request arguments for recovery.
  Reuse import keys only for identical bytes and metadata. Do not re-import
  after a timeout merely because no response arrived.
- New sources may use server-generated identities. To add a version to an
  existing document, first inspect its current version, then supply BOTH
  `--source-id` and the next `--source-version`; never replace history or
  reinterpret a conflict as permission to make a duplicate.

```bash
"$CLI" documents import "/path/to/original.pdf" \
  --idempotency-key unique-import-key --json
"$CLI" documents show SOURCE_ID --version VERSION --json
"$CLI" operations get OPERATION_ID --json
```

For stdin supply `--filename original.txt`. Optional `--provenance` is JSON
with only `source`, `source_ref`, `captured_at`, and `basename`; do not invent
values. Paths (including slash-bearing URLs) are rejected in the first three
fields; `basename` is a filename only. Never include credentials. `--mime` is
optional; byte/format validation still applies.

Supported extraction: TXT, Markdown, PDF, DOCX, XLSX, PPTX, PNG, JPEG, TIFF.
PDF extraction allows up to 5000 pages and a bounded 600-second worker runtime;
byte, pixel, text, and memory limits still apply. One unreadable page, OCR
timeout, oversized page or empty TIFF frame never fails the document: it is
flagged and the rest is kept. A wholly unreadable PDF fails unless the owner's
vision model reads it.
Use `status.max_file_bytes` for the current limit (default 16 MiB), not the
CLI's larger absolute ceiling. Do not silently truncate, split, decrypt, or
convert an original to bypass rejection. Handwriting, photos, diagrams, charts
and tables stored as images are read only when the owner configured a `vision`
block; otherwise they are listed as unread. Audio/video recognition and URL
fetching are not built-in ingestion capabilities. If another tool is needed,
disclose what it actually produces and preserve provenance.

## Verify each stage, not just acceptance

Inspect `documents show` and the operation result. Distinguish custody
`accepted` from extraction, indexing, wiki, and projection `completed`.
A failed later stage does not discard an accepted original. Report actual
state/error_code; readiness or exit code alone is not proof of completion.
The public document response contains stage metadata, the latest
`extraction_warnings` and a `coverage` summary, not full extracted text,
extraction records, or detailed extraction reasons. `coverage.complete: true`
means every page or frame was read and nothing on it was skipped;
`coverage.unresolved` lists pages with no text at all and `coverage.partial`
lists items with something unread (a figure, a picture, a suspect text layer).
Report both lists instead of calling the document fully read. Generic stage
errors must not be presented as a confirmed parser/OCR diagnosis.

Verify original custody by downloading to a NEW private temporary destination
and comparing SHA-256 with the submitted file, then remove the temporary copy:

```bash
"$CLI" documents download SOURCE_ID "/private/temp/new-original.pdf" \
  --version VERSION --json
```

Never overwrite a user file. `evidence --include-content` is for journal text,
not document originals. Do not read custody/catalog/derived storage directly
or query Hindsight internals to get around a missing public API.

## OCR and extraction recovery

PDF pages use native text first; only pages with no non-whitespace text fall
back to rasterization and OCR, after a blank-page check. Images use OCR
(including TIFF frames).
OCR is local Tesseract, discovered on service startup with `shutil.which`.
PDF rendering uses 150 DPI; each OCR call has a 30-second deadline, with
additional worker/resource limits. Inspect `documents show.extraction_warnings`:

| Warning | Meaning |
|---------|---------|
| `ocr_page:N` | Page N was read by OCR, which may contain errors. |
| `blank_page:N`, `blank_frame:N` | No visible content; nothing was lost. |
| `ocr_no_text_page:N`, `ocr_failed_page:N; <reason>`, `ocr_unavailable_page:N` (and `_frame` forms) | Page N remains unresolved; never treat it as read. |
| `figure_not_read:N` | Page N's text was read, but its images or drawings were not. |
| `text_layer_suspect:N` | Page N's text layer may be garbled (broken glyphs, mixed scripts). |
| `embedded_image_not_read:picture:N`, `chart_not_read:...` | A picture or chart in a DOCX, PPTX or XLSX was not read. |
| `vision_page:N`, `vision_figure:N`, `vision_frame:N`, `vision_picture:N` | The owner's vision model read the item. Its text starts with a `[Model-read ...; verify against the original]` label. |
| `vision_skipped:...`, `vision_refused:...`, `vision_failed:...`, `vision_blocked; <code>` | Vision was configured but did not read the item; the original warning stays. |
| `vision_opted_out` | The document was imported or rerun with `--no-vision`; it is never sent to a vision model. |

A very long ledger (more than 900 entries) is compacted: entries that share a
code and detail become one item list with page ranges, for example
`figure_not_read:12-15,20,31; ...`. Every listed page still counts.

Visually check names, dates, amounts, and other consequential details against
the original when creating an output, especially model-read text. Do not claim
verified transcription from stage completion alone.
OCR uses the installed subset of English, Russian, and Ukrainian language
data; English-only installations remain supported. There is no exposed
per-document language selector.

If scanned/image extraction is blocked, check `command -v tesseract` and
`tesseract --list-langs`. The internal `ocr_backend_not_configured` reason is
not exposed by the public document response; a generic blocked stage alone
does not prove that specific cause. If absent, report the precise missing dependency;
do not install it or restart anything. Vision is the owner's decision: never add or
change a `vision` block, set a vision key, or send page images to any service
yourself. Pass `--no-vision` on import when the owner asks to keep a document away
from the vision model. Encrypted PDFs, unsupported formats, malformed input, or resource
ceilings need a different input or an owner decision, not repeated retries.

After the owner has fixed an extraction problem and asks for it, request a new
extraction revision:

```bash
"$CLI" documents retry OPERATION_ID --rerun-extraction \
  --idempotency-key unique-extraction-retry-key --json
```

For transient indexing failures in an incomplete, recoverable operation, retry
WITHOUT `--rerun-extraction`, with its own stable mutation key. The same applies
when the vision endpoint was unreachable (extraction `failed` with
`provider_unavailable`): a plain retry resumes the same revision and asks the
model only about pages it has not read yet. Inspect the
operation first: it may be `completed` despite wiki/projection failure, and
retry then does nothing. Use reconciliation for projection-only failures;
use standalone rebuild below for wiki-only failures. After an
uncertain import outcome, replay the identical keyed import or inspect its
known operation before another mutation. Do not repeat an extraction rerun
blindly: the retry endpoint allocates a new extraction revision and does not
consume the CLI idempotency header. First inspect the existing document/operation.
A timed-out synchronous retain may still run remotely; matching provider
memory counts can be partial. Report uncertainty rather than certifying full
indexing while that retain is still processing. Allow one identical-key retry for a
transient provider error; if it still fails, report partial success and the remaining
error. No duplicate capture, alternate instance, direct provider calls, or
credential/authorization bypass. Rerunning extraction can make dependent wiki
pages stale; recheck them afterward.

## Wiki maintenance and categories

Document import attempts indexing, classification, wiki generation, and
projection automatically. Inspect the resulting pages and history:

```bash
"$CLI" wiki status --json
"$CLI" wiki list --json
"$CLI" wiki show PAGE_ID --json
"$CLI" wiki history PAGE_ID --json
```

Read the resulting managed text and citations; match the intended source
ID/version. `semantic_support_verified: false` means the system does not
certify semantic entailment; it is not a service-down flag. The service compiles supplied evidence with the configured LLM without
agentic retrieval. A rejected model response can be regenerated once for the
same window after rechecking the source; failed checks are never bypassed.
Claims are validated literal source quotes. Each document has its own managed page
(`source_<SOURCE_ID>`) with at most 48 claims sampled across the whole document, and
`index` lists the document pages; do not truncate sources or existing claims to
force a rebuild. Contradictions must stay visible; do not
choose a winner without evidence. Stale pages are not current truth.

To refresh an indexed document without reingestion:

```bash
"$CLI" wiki rebuild --source-id SOURCE_ID --version VERSION \
  --idempotency-key unique-wiki-rebuild-key --json
```

Omit `--version` to use the current version; omit both selectors to rebuild
active, current, indexed documents. Bulk rebuild rejects an oversized inventory
or more than 100 eligible documents rather than silently truncating it.
Historical, forgotten/deleted, and unindexed versions are excluded from bulk
rebuild and rejected as explicit targets.

Inspect returned operation `state`, `summary.error_code`, source identities,
and `item_count`, then read resulting pages. Admitted failures return an
operation ID even when some revisions were published. Replaying the same key
returns the saved status without another LLM call; it is not a retry. For a
transient provider failure, diagnose the cause and use a new rebuild key once.
If publication succeeded but projection failed, reconcile first rather than
republishing. An operation stuck `running` after a crash needs inspection;
replay does not force it to run again. Document retry of a completed operation
is also a no-op; use standalone rebuild for wiki-only failures.

Use `"$CLI" reconcile --json` to repair local projections from existing
revisions; this does not generate new wiki content. After forgetting or
deleting a document, run reconcile so every wiki page and history file drops its
text (pages left with nothing show as withdrawn). Never edit managed
Markdown, ownership markers, or user sections to force an update. Preserve
user edits and report projection conflicts. Through the agent gate `reconcile` is refused; report the projection failure
to the owner instead.

Classification may propose categories; it cannot approve them automatically.
Inspect `categories list` and `categories proposals --json`. User-requested
imports do not authorize category approval, budget overrides, merges, or
relationship approval. For an explicit category decision, use
`categories COMMAND --help`, current proposal/category IDs and revision gates.
The CLI supports no direct arbitrary wiki page/patch write; do not invent one.

## Health check and the review queue

`wiki lint` reports wiki and document health without a model call and changes
nothing:

```bash
"$CLI" wiki lint --json
"$CLI" wiki lint --fail-on error --json   # exit 3 when an error is found
```

Each finding has `code`, `severity` (`error`, `warning`, `info`), `page_id` or
`source_id`, a short `detail` and a `hint` naming a command. At most 500 are
listed, errors first; `omitted` counts the rest. Report findings; follow a hint
only when it reads, or when the owner asked for that fix.

Contradictions and hand edits of managed text wait in the review queue. List
and report them freely:

```bash
"$CLI" wiki reviews --json                         # pending, the default
"$CLI" wiki reviews --page PAGE_ID --state resolved --json
```

Resolving a review is the owner's decision. Run it only when the owner has
decided that specific review, and put the owner's decision in the note; never
resolve on your own judgement or to make lint pass:

```bash
"$CLI" wiki reviews resolve REVIEW_ID --note "Owner's decision" \
  --idempotency-key unique-resolve-key --json
```

Resolving records the decision and note only: claims are never edited and no
side is chosen; a resolved contradiction is not carried into the page's next
revision. Resolving the same review again is a `conflict`; replaying the same
key returns the saved result.

## Daily brief and change log

`brief` summarizes, without a model call, what the system did on one day in
the configured `timezone` (default today) or since a moment, and what is open:
documents imported with their stages, journal entries retained, wiki revisions
with claims added and removed, new contradictions, stale or conflicted pages,
unfinished document stages, pending reviews and lint counts.

```bash
"$CLI" brief --json
"$CLI" brief --date 2026-10-16 --json
"$CLI" brief --since 2026-10-16T08:00:00+02:00 --json
```

It only reads. `--project` (a day, not `--since`) also writes
`wiki/Briefs/YYYY-MM-DD - Brief.md` and `wiki/Log.md`, the last 200 wiki
revisions and document imports, newest first. Both are generated: an edited
copy is a projection conflict and is never overwritten. Use `--project` only
when the owner wants the files.

## Create something from the information

For “add this and make a summary/timeline/plan”, finish ingestion verification,
then create the requested output without asking again. Use `wiki show` plus
attributed `recall`/`reflect`, and resolve supporting document originals through
`documents download` when exact detail is needed. Ranked recall is not an
exhaustive inventory; document lists are bounded and have no pagination option.
There is currently no CLI for reading the complete extracted-text artifact.
Use available document-reading tools on the downloaded original as necessary;
do not pretend a truncated response or an OCR warning is complete evidence.

Cite source ID, version, and page/passage locator when available. Separate
quoted facts, synthesis, recommendations, and open questions. Do not invent
missing dates, commitments, or facts. If indexing/wiki is unavailable, a
requested output may still use the supplied original, but explicitly say
that it was not verified against the maintained wiki.

Default to delivering the output in chat. If the user requests a saved file,
write to their specified location or a new clearly named local output outside
managed projections; protect existing files from overwrite. Do not ingest
agent-generated analysis back as original evidence. If explicitly asked to
add the generated output to the wiki, import it as a distinct document with
clear generated-content provenance and original-source citations in its text.
The system will treat its text as evidence, not independently verified facts.
Do not promise arbitrary managed page titles/content: each document gets its own
page, and the model routes that page's checked claims onto shared topic/entity
pages without writing new quotes; the index is generated, not written by the model.

End with the source identity, actual stage results, produced output/location,
and any unresolved issue. Never report “wiki updated” solely from custody.
