# LifeContext operations

Configuration, startup checks, recovery and document handling for a local
LifeContext. Commands below use the installed `lifecontext` command, which
talks to the API on loopback port 8765 by default (`--port` selects another).
In the development checkout use `bin/lifecontext-dev` instead, which pins port
18765; see [development.md](development.md). How the parts fit together is in
[architecture.md](architecture.md); what leaves the machine is in
[privacy.md](privacy.md).

## Choosing the model

LifeContext uses one model at a time, and the text of your documents and notes
is sent to it. There is no default: in a terminal, the installer and
`lifecontext init` ask

```
Which model should LifeContext use? The text of your documents and notes is sent to it.
  1) Anthropic (API key)
  2) OpenAI (API key)
  3) Google Gemini (API key)
  4) OpenRouter (API key)
  5) A local model server (Ollama, vLLM, LM Studio)
  6) Another OpenAI-compatible endpoint
Choose 1-6:
```

and anything but a number from 1 to 6 asks again, up to three times. Choices 1 to 4 select the native
providers `anthropic`, `openai`, `gemini` and `openrouter`. Choice 5 selects
`openai_compatible` and suggests `http://127.0.0.1:11434/v1`, Ollama's address
(vLLM and LM Studio listen on other ports); a server on this machine needs no
key. Choice 6 selects `openai_compatible` and asks for the endpoint's URL. Then
type the exact model ID your provider uses; the provider's model list has them.
The other provider IDs in [Required LLM configuration](#required-llm-configuration)
are available through `--provider` and `LIFECONTEXT_PROVIDER`.

With Anthropic, `claude-sonnet-5` is expected to work (not yet qualified end to end) and `claude-haiku-5-5` costs less.
`claude-sonnet-5-5`, `claude-opus-5-5`, `claude-fable-5-1` and
`claude-mythos-5-1` do not work yet: LifeContext's retain step uses strict
structured output, which on Anthropic is a forced tool call, and these models
refuse a forced tool choice. `init` and `llm set` refuse them for the native Anthropic
adapter and warn when the same IDs are reached through `openai_compatible`; `doctor`
warns about an existing `config.yaml` that names one.

A run without a terminal never guesses: it needs the provider and the model as
flags or as `LIFECONTEXT_PROVIDER` and `LIFECONTEXT_MODEL`, and stops with
`provider_required` or `model_required` otherwise.

### Change the model or endpoint later

```bash
lifecontext llm show                       # provider, model, base URL, whether a key is stored
lifecontext llm set --model MODEL          # change only what you name
lifecontext llm set --provider openai --model MODEL --api-key-stdin < ~/openai-key.txt
lifecontext llm set --provider openai_compatible --base-url https://HOST/v1 --model MODEL
lifecontext llm set --api-key-stdin < ~/new-key.txt   # replace the key (or set LIFECONTEXT_API_KEY_FILE)
lifecontext preflight --model-probe        # a short check of the new settings
```

`llm show` never prints the key. `llm set` changes only the fields you give,
checks them the way `init` does, leaves every other setting alone and, when the
user service runs, restarts it so the new settings take effect; where no
service manager is available it prints the exact restart command. A new
provider needs `--model` too, and `openai_compatible` needs `--base-url`.
[Applying changed model settings](#applying-changed-model-settings) has the
details.

## Setting up with `lifecontext init`

```bash
lifecontext init                     # asks; the key is read with a hidden prompt
lifecontext init --non-interactive --api-key-stdin \
  --provider anthropic --model claude-sonnet-5 --timezone Europe/Vienna < key.txt
```

`init` creates `~/.lifeweave` (mode `0700`), writes `config.yaml` with the `llm`,
`vault` and `timezone` blocks described below and `.env` with
`LIFEWEAVE_LLM_API_KEY` (both mode `0600`), and creates the vault root, its
`Journal` directory, `Journal/entries` and `Journal/Inbox` (mode `0700`; an
existing directory you own is tightened to `0700`). The provider and the model
have no default ([Choosing the model](#choosing-the-model)); the vault defaults
to `~/LifeContext/vault` and the time zone to the system's (from `TZ`,
`/etc/timezone` or the `/etc/localtime` link, else `UTC`). Other flags:
`--base-url`, `--vault DIR`, `--non-interactive`, `--json`.

- It is idempotent: an existing `config.yaml` and key are kept. When a flag
  asks for something else than the existing `config.yaml` holds, the report
  carries the warning `flags_ignored` and lists them in `ignored`.
- `--force` applies the flags you give (or, in a terminal, your answers) to the
  existing `config.yaml` and keeps every other setting: the vault and its
  `relationship_roots`, `vision`, `limits` and the time zone stay unless a flag
  changes them. Changing `--provider` starts the `llm` block from that
  provider's defaults. Comments in `config.yaml` are not kept. To start over,
  move `config.yaml` away first. `--force` never touches `.env`.
- `--replace-key` replaces the stored model key with the one given and changes
  only `.env` (a vision key there is kept); without a key on stdin, in
  `LIFECONTEXT_API_KEY_FILE` or at the prompt it fails with `api_key_required`.
  Without it, a key given while `.env` already has one is not saved and the
  report warns `existing_key_kept`.
- The key is read only from the hidden prompt, with `--api-key-stdin` from one
  line on stdin, or from the file named by `LIFECONTEXT_API_KEY_FILE` (one
  line; a container secret works; the report warns
  `api_key_file_readable_by_others` when group or others may read it); there is
  no flag that takes it on the command line, and it is never printed. Without a key, `init` still sets up everything
  else, reports the key as `missing` and exits 0; run it again with the key.
- Unattended installs can pass `LIFECONTEXT_PROVIDER`, `LIFECONTEXT_MODEL`,
  `LIFECONTEXT_BASE_URL` and `LIFECONTEXT_VAULT` instead of the flags; a flag
  wins over its variable.
- A vault given with `--vault` through a symbolic link is stored as its real
  path, because the service opens every vault folder without following links.
  If `config.yaml` names a vault path with a link anywhere in it, `init` stops
  with `vault_path_has_symlink`; `init --force` stores the real path. A vault
  check or a key that fails leaves `config.yaml` as it was.
- Exit codes: 0 when set up (also when only the key is missing), 1 with
  `{"error": CODE, "message": ..., "next_step": ..., "retryable": ...,
  "request_id": null}` under `--json` (see [Failure codes](#failure-codes)),
  2 for a usage error.
- An `openai_compatible` endpoint needs no key (see [API key](#api-key)).
- `--non-interactive` without a provider or a model stops with
  `provider_required` or `model_required`; four Claude models are refused for
  the native Anthropic adapter ([Choosing the model](#choosing-the-model)).
- In a terminal, after writing the configuration, `init` checks the model
  with two short calls (the model probe below) and reports it as `model_probe` (`status` and a `cause`, never
  the provider's own text); a failed probe does not undo the setup. Run
  `lifecontext preflight --model-probe` to check again.
- It never starts the API or Hindsight. Its report lists `next_steps` for you
  (open the vault in Obsidian, import a document, ask your agent) and
  `setup_steps` for a source install (`preflight`, `service install
  --maintenance`, `skills install`, `doctor`), which `install.sh` has already
  run.

## Diagnostics with `lifecontext doctor`

```bash
lifecontext doctor           # readable summary
lifecontext doctor --json    # the full report
```

`doctor` only reads, and makes no model call unless you add `--model-probe`
(see [Startup checks](#startup-checks)). It reports the LifeContext, platform and Python versions;
the pinned Hindsight package versions; `command -v lifecontext` (the absolute
path to put in MCP client configs); the paths and modes of `~/.lifeweave`,
`config.yaml`, `.env`, the vault root and `Journal`; a config summary (provider,
model, base URL, time zone, which limits are customised) and whether the keys are
present, never their values; the service on `--port` (`/healthz`, then
`/v1/status` and the wiki lint counts with the local token), including how many
journal entries are waiting for indexing (the count only: `lifecontext status`
lists them); unfinished and
failed operation counts from a read-only catalog connection; Tesseract, its
version and installed languages (`eng`, `rus`, `ukr` are expected); the vision
configuration (it never sends the probe image: use `preflight --vision-probe`);
the Hugging Face cache state of both models, whether the next daemon start will
be offline (`HF_HUB_OFFLINE=1`), whether the last one was, and the tokenizer
cache; the Hindsight engine (`engine`: whether the profile turns token auth on
and holds the current token, whether the daemon on the profile's port is this
HOME's, another account's or HOME's, or not running, and, for this HOME's,
whether it refuses a request without the token; whether Postgres runs and
listens on its socket only or on TCP); and free disk space. It prints no
journal, document or wiki content, and no key, token or password. It does
name vault-relative paths: `vault_refusals` lists (up to 20) the folders and
files below `Journal`, and the `.journal.lock` file, that a vault write would
refuse (a symlink, a special file, an entry owned by another user, a folder
that group or others can write). Those paths are often document titles. The
scan is read-only, does not enter a folder on another device than `Journal`
(it lists those under `skipped`), and stops after 200,000 entries (`truncated`). `status` names the last refused path as
`dependencies.vault.refused_path` until it is acceptable again, and its plain
output prints it on a `Refused vault path:` line. The MCP `status` tool never
passes the path (it can name a journal note): it says only that a path was
refused (`path_refused`) and that `lifecontext status` in a terminal names it. A command that fails with
`unsafe_path` or `unsafe_permissions` names that path in its plain message too
(read from `status`; never in the API error body or under `--json`).

It exits 1 only for broken prerequisites, listed in `problems`:
`python_unsupported`, `hindsight_packages`, `home_missing`, `home_unsafe`,
`config_missing`, `config_invalid`, `credentials_missing`, `credentials_unsafe`,
`credentials_invalid`, `vault_missing`, `vault_unsafe`, `vision_blocked`,
`engine_auth_off` (the daemon does not require the token; restart the service,
see [Engine access](#engine-access-token-port-and-postgres)), `engine_port_taken`
(something else listens on the profile's port), `postgres_tcp` (Postgres
listens on TCP or the profile is set up for it), `postgres_files_unsafe` (the
socket directory is not `0700` or not `~/.lifeweave/postgres`, or
`instance.json` is not a private file of yours), `engine_profile_unsafe`, and
with `--model-probe`, `model_probe_failed` (the report's `model_probe` says
why). `warnings` do not change the exit code: `service_not_running`,
`service_not_ready`, `tesseract_missing`, `tesseract_languages_missing`,
`models_not_cached` (normal before the first start), `disk_low` (under 2 GB),
`model_rejects_forced_tool_use` (`config.yaml` names one of the Claude models
that do not work yet), `vault_files_refused` (`vault_refusals` lists paths a
write would refuse; other files in the vault are never reported),
`model_uses_plain_http` (the model server is reached over plain HTTP on a host
other than this machine) and, with `--model-probe`, `model_reflect_check_failed`.

## Required LLM configuration

The only supported settings file is `~/.lifeweave/config.yaml`. Its `llm`
block is exactly (the `vault`, `timezone` and optional `vision` blocks are
described below):

```yaml
llm:
  provider: <provider ID>
  model: <exact model ID>
  base_url: <provider API base URL>
  reasoning_effort: high  # optional: low, medium, high, or xhigh
  allow_insecure_http: <the same URL as base_url>  # optional: see the URL rules below
```

`provider` and `model` are required. `base_url` is required for
`openai_compatible` and `volcano`; it is optional for other native providers
that have a built-in endpoint. Use the exact lowercase provider ID:

- `openai_compatible`: the OpenAI Chat Completions protocol, not a vendor; use
  it for any service that implements it, such as a local Ollama, vLLM or LM
  Studio server. LifeContext maps it to Hindsight's `openai` adapter. Supply
  that service's API root in `base_url` (usually ending `/v1`), not its
  `/chat/completions` endpoint.
- Native adapters: `openai`, `openai-responses`, `anthropic`, `gemini`,
  `groq`, `openrouter`, `minimax`, `ollama-cloud`, `deepseek`, `volcano`,
  `requesty`, `zai`, `opencode-go`, `atlas`, `fireworks`.

`model` must be the exact ID expected by the selected provider. For native
providers, omit `base_url` to use the adapter's built-in endpoint, or set the
provider's API root when its adapter supports a custom endpoint. Hindsight
0.9.2's native Gemini adapter ignores custom URLs; LifeContext rejects `base_url` with
`provider: gemini` so the setting cannot be silently ignored.

`reasoning_effort` is optional and accepts exactly `low`, `medium`, `high`, or
`xhigh`. If omitted, the provider/model uses its default. When set, it becomes
Hindsight's global LLM effort setting; select a value supported by the chosen
model and endpoint. The pinned native Anthropic and Gemini adapters do not
apply it, so LifeContext rejects it for those providers. It configures request effort
and does not expose private chain-of-thought text. Hindsight may omit it for
model IDs it recognizes as incompatible, and a custom endpoint may reject a
value it does not support.

LifeContext fixes Hindsight's fact-extraction output limit at 8,192 tokens so its
default does not exceed common provider limits. The selected model endpoint
must accept that output limit; this is not a separate user config field.

LifeContext supports one selected provider, one model, at most one custom
URL and one key at a time; there is no provider fallback and no
provider-specific key lookup. It rejects unknown provider IDs, unknown fields inside `llm`, old
`hindsight.llm_provider`/`llm_model`/`llm_base_url` fields, invalid URLs, and
credentials in YAML.

Remote URLs must use HTTPS. Plain HTTP is accepted without a flag only for
loopback (`localhost`, `127.0.0.0/8`, `::1`), for IP addresses written out in
`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10` (Tailscale and
carrier-grade NAT) or `fc00::/7`, and, inside the Docker image only (it sets
`LIFECONTEXT_IN_CONTAINER=1`), for the name `host.docker.internal`. A name is
never looked up, so any other name (`models.lan`), and `host.docker.internal`
outside the image, is refused over HTTP, and so are `169.254.0.0/16`,
`fe80::/10`, `0.0.0.0` and `::`. This applies to the `llm` block only: `vision`
accepts plain HTTP for loopback only. Any other HTTP host needs a grant:
`allow_insecure_http` holds the exact URL it was given for, which `lifecontext
init --allow-insecure-http` and `lifecontext llm set --allow-insecure-http`
write, and it counts only while `base_url` is still exactly that string. Edit
`base_url` by hand and the grant stops applying, so the service refuses to start
until you grant the new URL; a `true` from an earlier draft names no URL and
grants nothing. The flag saves nothing for a URL that needs none (HTTPS, loopback,
a private address). Over HTTP the text of your documents and notes, and the key
if one is stored, travel unencrypted on the network: init and `llm set` print that,
`llm show` lists the grant, and `doctor` reports the warning
`model_uses_plain_http` for any non-loopback HTTP endpoint. URLs cannot contain
credentials, query strings, or fragments. Never put a key in `base_url`.

## API key

Put the API key matching the selected provider in the private file
`~/.lifeweave/.env`:

```dotenv
LIFEWEAVE_LLM_API_KEY=<your provider API key>
```

`.env` holds at most two keys: `LIFEWEAVE_LLM_API_KEY` for the `llm` provider
and, optionally, `LIFEWEAVE_VISION_API_KEY` for the vision model (see below).
LifeContext requires a non-empty LLM key for every provider except
`openai_compatible`: an Ollama, llama.cpp or vLLM server usually needs none,
`.env` may then be absent, and Hindsight gets the placeholder key `local`. A key in `.env` is still
used when present. It does not read provider-specific variables
(`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `HINDSIGHT_LLM_API_KEY`), Hermes
credentials, or ambient shell variables. The key file must be owned by the
current user and mode `0600` on POSIX systems; the containing directory must be
mode `0700`. LifeContext creates `~/.lifeweave` with mode `0700` when it is
missing and tightens an existing one to `0700`; it refuses a directory owned by
someone else.

LifeContext maps the configured provider, model, URL, and key into the isolated
Hindsight profile. The Hindsight profile file is generated and should not be
edited by hand.

`config.yaml` itself must also be owned by the current user and mode `0600`.

## Vault, time zone and limits

The managed Markdown projection lives in an Obsidian-compatible vault. Without a
`vault` block, the service uses `~/LifeContext/vault` as the vault root and
`~/LifeContext/vault/Journal` as the journal path, where `~` is the HOME of the
process that runs `lifecontext serve`. To use another vault, name both paths:

```yaml
vault:
  root: /home/you/Notes
  journal_path: /home/you/Notes/Journal
  # relationship_roots: [Areas, Projects]   # optional
timezone: Europe/Vienna   # optional
```

- `root` and `journal_path` are absolute paths (no `~`), and `journal_path` lies
  inside `root`. Both directories, including the default ones, must exist
  before the service starts, be owned by the current user and have mode `0700`;
  the service does not create or repair them (`lifecontext init` creates them).
- LifeContext writes below `journal_path`, into `entries/`, `documents/`,
  `wiki/`, `relationships/`, `indices/` and `reports/`, and keeps its writer
  lock `.journal.lock` in `root`. `wiki/SCHEMA.md` and `expectations/` are
  read-only to it.
- `relationship_roots` optionally lists up to 128 vault-relative folders that
  `relationships scan` may read when proposing links. A folder may not be
  hidden or contain a hidden component, may not start with a `journal` folder
  in any letter case, and may not be nested in another listed folder.
- `timezone` is an IANA name such as `Europe/Vienna` (it needs the system time
  zone database). It sets the day used for journal file names; the default is
  the service's local time zone.

An optional `limits` block adjusts request and scan bounds. Each value is an
integer from 1 to its ceiling; unknown names are rejected:

| Limit | Default | Ceiling |
|---|---|---|
| `category_budget` | 10 | 100 |
| `max_text_bytes` | 1,048,576 | 1,048,576 |
| `max_file_bytes` | 16,777,216 | 67,108,864 |
| `max_query_bytes` | 8,192 | 65,536 |
| `max_result_count` | 20 | 100 |
| `max_response_bytes` | 1,048,576 | 4,194,304 |
| `max_relationship_files` | 2,000 | 20,000 |
| `max_relationship_file_bytes` | 1,048,576 | 16,777,216 |
| `max_relationship_bytes` | 16,777,216 | 67,108,864 |
| `max_relationship_scan_ms` | 2,000 | 15,000 |

## Optional vision model for pictures, handwriting and charts

Text layers and local Tesseract OCR read typed text. Everything else on a page
(handwriting, photos, diagrams, charts, tables stored as images) is listed in
`documents show` as unresolved or partial unless you add a `vision` block:

```yaml
vision:
  provider: openai_compatible   # openai_compatible | openai | anthropic | gemini
  model: <exact vision model ID>
  base_url: http://127.0.0.1:11434/v1   # required for openai_compatible
  max_pages_per_document: 200            # optional, 1-5000
```

No block means vision is off; it never inherits from `llm`, because sending page
images is a separate decision. When it is on, a local router sends only the
items that need it (pages without a usable text layer, figures on text pages,
embedded pictures) as JPEGs of at most 2000 px to that endpoint. Everything else
stays on the machine. `base_url` follows the same rules as for `llm`, except that plain HTTP is
accepted for loopback only (no private-network addresses, no
`host.docker.internal`, no `--allow-insecure-http`); `gemini`
and `anthropic` may also set one for a compatible gateway.

The key is `LIFEWEAVE_VISION_API_KEY` in `~/.lifeweave/.env`. Without it, the LLM
key is reused only when `provider` and `base_url` equal the `llm` block's. A
loopback `openai_compatible` server such as Ollama or llama.cpp needs no key.

The block is read when the service starts. Before the first image, the service
asks the model to read a generated test code; if it cannot, vision is marked
`vision_model_unusable` and extraction continues without it. Check the setup
with `lifecontext preflight --vision-probe` (this sends one generated image).
The probe proves the model can see, not that it reads your handwriting well;
compare a few model-read pages with the originals before relying on them.

Every model reading is labelled `[Model-read ...; verify against the original]`
in its text, tagged `vision:<model>` in its locator and recorded as
`vision_page:N` (or `_figure`, `_frame`, `_picture`) in the warnings, and model
text is indexed in its own chunks. Readings are cached per source under
`~/.lifeweave/vision-cache/` and deleted with the source. If the endpoint is
unreachable, the import fails retryably; `documents retry SOURCE_ID` resumes
the same extraction revision from the cache. Changing the model never rewrites
existing documents; `documents retry SOURCE_ID --rerun-extraction` re-reads
them, and only the routed pages call the new model.

An item the model refuses, or answers twice with unusable JSON, stays unresolved
(`vision_refused:...`, `vision_failed:...`) and the document completes without it.
A plain retry does not ask again. `documents retry SOURCE_ID
--rerun-extraction` makes a new extraction revision with its own re-index: it
asks again for `vision_failed` items, while a refusal stays cached and is not
asked again until the vision provider or model changes.

On start, the service resumes unfinished imports, including their vision reads,
before it answers requests, as it already does for unfinished indexing. After a
long outage with several pending documents, startup can take minutes per
document; the service's log shows that it is busy, not hung. Under
`lifecontext service install --maintenance` the log is in `journalctl --user -u lifecontext`
on Linux and `~/Library/Logs/lifecontext/` on macOS (see
[Running as a user service](#running-as-a-user-service)).

`documents import --no-vision` keeps a document away from vision for good,
including its later versions; the choice is recorded in its extraction ledger
and honoured by recovery and every rerun. `documents retry ... --rerun-extraction
--vision` lifts it.

### A failed or partial import, and retrying it

An operation can be `completed` while the document's wiki page or vault note did
not come out. The document and operation responses therefore carry an `outcome`
beside the unchanged catalog `state`: `completed` (every stage is done),
`completed_with_errors` (the import finished but the wiki or vault stage failed),
`running` (the service is still running a configured wiki or vault stage), or the
operation's own state (`pending`, `retryable_failed`, `permanent_failed`,
`cancelled`). A wiki or vault stage that is pending or running with nothing
running it, for example a vault note a category rename, merge or reassignment
put back to pending, makes the outcome `completed_with_errors`; it is listed in
`problems` with its state (`pending` or `running`) as the `code` and
`lifecontext documents retry SOURCE_ID` as the `next_step`. A document also lists
`problems` (each with `stage`, `code`, `reason` and the `next_step` to run), its
`last_operation_id`, and, once the page's note is in the vault, `wiki_page_id` and
`vault_path` (from the vault root, starting with the configured Journal folder,
for example `Journal/wiki/Lease.md`). `lifecontext operations get OPERATION_ID --json`
and `GET /v1/operations` show the same `outcome` for document imports and retries;
`GET /v1/operations?state=retryable_failed` or `?outcome=completed_with_errors`
(among the newest 1000 operations) finds what a retry can pick up;
`lifecontext operations list` asks the same (`--state STATE`, `--outcome OUTCOME`,
`--limit N` up to 1000, default 100) and prints a table of operation ID, kind,
state, outcome, source ID and last update, or the API's answer unchanged with
`--json`. `lifecontext doctor` counts a failed import, retry or rebuild until a
later one completes for each of its documents at the same or a newer version.

`documents import` and `documents retry` exit with status 4 when the answer's
`outcome` is not `completed` (`completed_with_errors`, `running`, or a failed
operation state); the `--json` output is the same as with status 0. Plain output
then lists each failed stage under `Problems:` and ends with a `Next:` line built
from their `next_step`, usually `lifecontext documents retry SOURCE_ID`. Status 1
stays a command that failed, and 3 is `wiki lint --fail-on`.

Before uploading, `documents import` refuses a file whose name does not end in
one of the extensions the extraction reads (`.docx`, `.jpeg`, `.jpg`,
`.markdown`, `.md`, `.pdf`, `.png`, `.pptx`, `.tif`, `.tiff`, `.txt`, `.xlsx`)
with `unsupported_format`, and a file above the service's `max_file_bytes` from
`lifecontext status` with `body_too_large`; each message names the formats or the
limit. When status cannot be read, the upload goes ahead and the service applies
its own limit.

`lifecontext documents retry SOURCE_ID` is the one command to run; the
operation ID of any of the document's operations works too. It runs again every
stage of the current version that is not completed: indexing, then the wiki page
and the vault note, so an indexing failure that is fixed also puts the page in the
vault. A completed stage is not repeated, and no retry, by source ID or by any of
its operation IDs, rebuilds the page or vault note of a version a newer one
replaced. If the last extraction could not read the file, the retry reads it
again; `--rerun-extraction` (with `--vision` or `--no-vision`) does it on demand.
A retry of a document that is still being processed by the running service (an
import, retry, reindex or `wiki rebuild` of it) does not start a second run: it
returns the operation's current state. A reindex or `wiki rebuild --source-id`
of such a document is refused with `conflict` before anything is stored under
its key; send the same request with the same key once it finishes. A `wiki
rebuild` of every document leaves such documents out, lists their source IDs
in `skipped` (in its first answer only) and keeps them out of the operation's
`summary.sources`. A deleted document is not found, and a forgotten one is not
retried.

Idempotency: without `--idempotency-key`, each retry is a new attempt, so a
failed wiki stage can be retried as often as needed, and the key a repeated
`--rerun-extraction` uses is fixed per revision, so it never creates two. With
`--idempotency-key KEY`, the key is binding: repeating the same key and request
returns the same retry (and resumes it if it did not finish) instead of running
it again, and the same key with a different request is refused as a conflict. Use
a fresh key for every deliberate new attempt.

A wiki stage that failed keeps its reason as the stage's error code:
`wiki_provider_failed` (the model call failed), `wiki_update_rejected` (the page
failed its checks), `wiki_interrupted` (an earlier run stopped part-way),
`wiki_evidence_limit` (one passage is too large for a model request) and
`wiki_no_evidence` (the document has no text to summarise). The last two come back
the same way on every retry, so their `next_step` suggests none. Any other failure
inside LifeContext reports `internal_error`. A wiki stage that completed without
some of its evidence windows (the model failed on them; see
[Document OCR and standalone wiki refresh](#document-ocr-and-standalone-wiki-refresh)) is listed in `problems` as
`wiki_windows_skipped`; `documents retry SOURCE_ID` compiles the page again and
asks the model only for those windows. A wiki stage that completed but whose
page has no model summary is listed in `problems` as `wiki_summary_missing`;
`documents retry SOURCE_ID` asks the model for the summary alone and publishes
the same claims with it, without reading the evidence again. A page keeps room
for the largest summary; one written by an earlier release that is too full for
one is not listed (`wiki lint` names it, and `wiki rebuild` compiles it again
with room). After a bundle restore, a document whose page came in the bundle has
a completed wiki stage, and the page is stale until `lifecontext maintain
--rebuild-stale N` or `wiki rebuild`; a document without one waits for a retry.

## Startup checks

```bash
lifecontext preflight
lifecontext preflight --model-probe
lifecontext preflight --retain-smoke
```

`preflight --model-probe` only calls the model: one small request through the
same structured-output path that retain uses for your provider, with a
60-second limit (one retry on a rate limit), plus a check of the named tool
call that reflect uses, which can only warn. It does not start the memory
engine. It prints `{"model_probe": {...}}` with `status` (`pass` or `fail`),
a `cause`, a `message` and the `fix` command for a failure, and `reflect`
(`pass`, `warn` or `skipped`), and exits 0 on a pass and 3 on a failure. The
causes: the provider rejected the key (`key_rejected`), no key is stored
(`key_missing`), the model was not found (`model_not_found`), the model does
not support the structured output LifeContext needs
(`structured_output_unsupported`), the provider is rate limiting the key
(`rate_limited`), the endpoint did not answer (`unreachable`), the settings
are not valid (`invalid_config`), or another failure (`failed`). It never
prints the provider's own error text. `doctor --model-probe` adds the same
check to the doctor report.

Plain `preflight` checks the configuration and that a key is stored, then
starts the local memory engine and checks that it answers. It does not check
the model: starting the engine sends it one tiny test request and only logs
the result. `--retain-smoke` also retains one test source, which goes through
the model and can take many minutes. These two commands use only the `lifeweave` Hindsight profile, bound to
`127.0.0.1`. They may start the profile and leave it running after the check;
an already-running profile is reused without reconfiguration. Restart the
profile before applying changed model settings. The JSON field `daemon_status`
reports `running`, `engine_auth` reports `enforced` and `postgres` reports
`socket_only`. The test source of `--retain-smoke` stays in a disposable bank
of the local Hindsight database. Both emit redacted JSON. They do not
launch a LifeContext HTTP API service.

### Engine access: token, port and Postgres

Each start (`preflight` or `serve`) makes the `lifeweave` profile private to
this account and HOME:

- **Token.** `~/.lifeweave/hindsight-api.token` (mode `0600`) holds a random
  token, renewed every time LifeContext starts the daemon. The profile turns on
  Hindsight's `ApiKeyTenantExtension` with it, so the daemon answers every
  memory request with 401 unless it carries the token; only `/health`,
  `/health/ready`, `/health/live`, `/version`, `/metrics` and
  `/v1/bank-template-schema` answer without it, and the daemon's MCP endpoint
  is turned off. LifeContext's client sends the token, never through a proxy
  (loopback is added to `NO_PROXY`). The start checks both: a request without
  the token must get 401 and one with it 200, else it fails with
  `engine_auth_disabled` or `engine_auth_rejected` and stops the daemon it
  started.
- **Port.** hindsight-embed derived the port from the profile name alone, so
  every HOME's `lifeweave` profile wanted `127.0.0.1:9855`. LifeContext now
  picks a free port in 20000-29999 from a hash of your account and HOME and
  keeps it in the profile. Before it sends anything to the port it checks who
  listens there (on Linux through `/proc`, which also tells this HOME's daemon
  from another HOME's of the same account; on macOS through `lsof`, which tells
  only the account). A listener that is not this HOME's daemon is never
  reused, stopped or sent the token: a start moves to another free port, and
  one that still finds a stranger there fails with `daemon_port_foreign`.
  While `serve` runs, every request to the daemon first checks that its port
  is still held by your account, so if the daemon dies and another user takes
  the port, requests fail instead of reaching them. When the daemon answers
  401 (a later `preflight` renewed the token), the service reads the token file
  again once. Another user can still keep LifeContext from starting by holding
  the ports it would pick.
- **Postgres.** LifeContext runs the profile's embedded PostgreSQL itself, with
  the binaries and settings pg0 uses, but with `listen_addresses=''`: it
  listens only on a Unix socket in `~/.lifeweave/postgres` (mode `0700`), and
  Hindsight connects through that socket. A new install creates the database
  through pg0 with a random password, which pg0 keeps in
  `~/.pg0/instances/hindsight-embed-lifeweave/instance.json` (made `0600`).
  pg0 can create a database only by starting PostgreSQL on `127.0.0.1`, so
  that first start listens on TCP for a few seconds; see
  [privacy.md](privacy.md#other-users-of-this-computer). After a restore, pg0
  unpacks the binaries by starting a throwaway instance with no TCP listener
  and its socket in `~/.lifeweave/postgres`, which is then deleted. Unlike
  pg0, LifeContext runs PostgreSQL with fsync on.

An install from before engine auth is moved over on its next start, without
touching its data: LifeContext stops the old daemon (only if it is this
HOME's), which stops the Postgres it started; starts PostgreSQL on the socket
with the database's existing password (`hindsight` for those installs; it no
longer matters, since nothing reaches the database over TCP); and starts the
daemon with the token on its new port. If a step fails, the start fails with
its code, stops the PostgreSQL it started, so nothing runs as before the move,
and the next start begins again; a daemon it started that does not refuse a
request without the token is stopped too. The database and the contents of
`instance.json` are never changed (the file only becomes `0600`). To go back
to an older LifeContext after the move, run `lifecontext hindsight stop` with
this version first: older versions start PostgreSQL through pg0, which fails
while it already runs, and then deletes `instance.json`. An install created by
this version cannot go back, since older versions do not know its database
password. The same happens when the token file was
deleted: a new token is made and the daemon restarted with it.

The socket path, `~/.lifeweave/postgres/.s.PGSQL.PORT`, must stay under 104
bytes, so a HOME path longer than about 68 characters fails with
`postgres_socket_path_too_long`.

### Downloads on first start, offline afterwards

When LifeContext starts the `lifeweave` Hindsight daemon (from `preflight` or
`serve`), it checks the Hugging Face cache (`HF_HUB_CACHE`, else `HF_HOME/hub`,
else `~/.cache/huggingface/hub`) for complete `main` snapshots of the embedding
model `BAAI/bge-small-en-v1.5` and the reranker
`nreimers/mmarco-mMiniLMv2-L6-H384-v1`. If both are there, the daemon starts with
`HF_HUB_OFFLINE=1` and makes no Hub calls; before the first download it starts
without it so the models can be fetched. Two more variables are always set:
`LITELLM_LOCAL_MODEL_COST_MAP=True` (litellm keeps its bundled price list
instead of fetching one from GitHub at import; Hindsight does not use prices)
and `TIKTOKEN_CACHE_DIR=~/.lifeweave/cache/tiktoken` (the tokenizer file is kept
across reboots instead of in `$TMPDIR`). hindsight-embed passes only `HINDSIGHT_*`
profile settings to the daemon, so these are set in the environment of the
process that starts it, not in the profile file. An already running daemon keeps
the environment it started with; `~/.lifeweave/hindsight-start.json` records
whether the last start was offline, and `lifecontext doctor` reports it with the
cache state.

The pinned `hindsight-all==0.9.2` runtime pulls a large ML dependency set, so
allow several GB for the Python environment. CI measured about 2.4 GB on Linux
x86_64 with CPU-only PyTorch; a development environment with the default CUDA
build of PyTorch was about 6 GB.

## Failure codes

The command exits nonzero and emits one stable `error` code for missing or
unsafe credentials, invalid config, missing or drifted Hindsight packages,
unsupported capabilities, profile setup, daemon readiness, or retain failures.
Reports never include config lines, profile contents, credential values, or
raw provider exceptions.

Under `--json` a failure is one object: `error` (the stable code), `message`
(one sentence), `next_step` (what to do, worded for the owner when the repair is
theirs), `retryable` and `request_id` (the service's request ID, or `null` when
the service did not answer). The plain message is the same sentence followed by a
`Next:` line. A call that gets no answer reports `api_unavailable`: a refused
connection says the service is not reachable; a timeout says it may still be
working, and it is `retryable` only for a read, because a write or a `recall`
that timed out may still finish.

The list below is generated from the table in `src/lifecontext/cli_output.py`
(`python -m lifecontext.cli_output --write docs/operations.md` rewrites it; a
test fails when the two differ).

<!-- failure-codes:begin -->

### The API's codes

- `invalid_request`: The request was refused: an argument or the input is not valid. Next: check the arguments with `lifecontext COMMAND --help`.
- `unauthorized`: The API token was refused or cannot be read. Next: run the command as the same user, with the same HOME, as the service.
- `body_too_large`: The input or the answer is larger than the configured limit. Next: send a smaller input; for recall and reflect lower --max-results or --max-tokens.
- `response_too_large`: The service's answer is larger than this command accepts. Next: ask for less: lower --limit, --max-results or --max-tokens.
- `host_not_allowed`: The service accepts requests only for 127.0.0.1. Next: connect to 127.0.0.1 directly, not through a proxy or another host name.
- `origin_not_allowed`: The service refuses requests from a web page. Next: call it from the CLI or an agent, not from a browser.
- `source_not_found`: No source or page has that ID. Next: `lifecontext documents list` and `lifecontext wiki list` show the IDs; a document's wiki page is `source_` followed by its ID.
- `source_conflict`: That source ID and version already hold different content. Next: use the next version number, or look at the stored one with `lifecontext documents show ID`.
- `evidence_unavailable`: The stored original of that source cannot be read or no longer matches its checksum. Next: run `lifecontext reconcile`; if it still fails, `lifecontext doctor` says what is wrong.
- `invalid_source_text`: The input must be UTF-8 text. Next: convert it to UTF-8, or import the file with `lifecontext documents import`.
- `invalid_provider_response`: The model answered, but not in the expected form. Next: check the model with `lifecontext doctor --model-probe`, then re-run the command.
- `provider_unavailable`: The model did not answer. Next: check the model with `lifecontext doctor --model-probe`, then re-run the command.
- `retain_failed`: The memory engine did not store the text. Next: re-run with the same --idempotency-key; `lifecontext doctor` checks the service, the engine and the model.
- `recall_failed`: The memory engine could not search. Next: re-run in a minute; `lifecontext doctor` checks the service, the engine and the model.
- `reflect_failed`: The memory engine could not write an answer. Next: re-run in a minute, or use `lifecontext recall`; `lifecontext doctor` checks the service, the engine and the model.
- `unattributed_evidence`: The answer could not be tied to a stored source, so it was withheld. Next: rephrase the question, or read the passages with `lifecontext recall`.
- `result_limit_exceeded`: More results were asked for than the configured maximum. Next: lower --max-results.
- `operation_status_unavailable`: The state of that operation cannot be read now. Next: check the operation ID and try again; `lifecontext doctor` checks the service, the engine and the model.
- `curation_unavailable`: That change to the source cannot be made now. Next: look at the current version with `lifecontext documents show ID`, then re-run.
- `transfer_unavailable`: The export or import bundle cannot be processed. Next: check the bundle file; `lifecontext doctor` checks the service, the engine and the model.
- `conflict`: It changed first: the revision or version you gave is no longer the current one. Next: read the current revision with the matching list or show command and re-run with it.
- `relationship_unavailable`: That relationship cannot be read or decided now. Next: see the current edges with `lifecontext relationships list`, then re-run.
- `unsafe_path`: The service refused a path outside the vault or through a symbolic link. Next: `lifecontext doctor` names the path to fix.
- `unsafe_permissions`: Files or folders of LifeContext are open to other users. Next: `lifecontext doctor` names them; make folders 0700 and files 0600.
- `vault_unavailable`: The vault cannot be read or written. Next: check that the vault folder exists and belongs to you; `lifecontext doctor` checks the service, the engine and the model.
- `catalog_busy`: The catalog is busy with another change. Next: wait a few seconds and re-run.
- `internal_error`: The service failed unexpectedly. Next: re-run once; if it fails again, run `lifecontext doctor` and look for the request ID in the service log.

### A document's wiki stage (its `error_code` and the `code` of a `problems` entry)

- `wiki_provider_failed`: The model did not answer while the document's wiki page was compiled. Next: check the model with `lifecontext doctor --model-probe`, then run `lifecontext documents retry SOURCE_ID`.
- `wiki_update_rejected`: The compiled wiki page failed its checks and was not saved. Next: run `lifecontext documents retry SOURCE_ID`.
- `wiki_interrupted`: An earlier wiki run for the document stopped part-way. Next: run `lifecontext documents retry SOURCE_ID`.
- `wiki_windows_skipped`: The wiki page was published without the parts of the document the model failed on. Next: run `lifecontext documents retry SOURCE_ID`; it asks only for those parts.
- `wiki_evidence_limit`: One passage of the document is too large for a single model request, so a retry fails the same way. Next: import a version with shorter passages.
- `wiki_no_evidence`: The document has no readable text to compile a wiki page from, so a retry fails the same way. Next: check what was read with `lifecontext documents show SOURCE_ID`.

### The CLI's own codes

- `api_unavailable`: The LifeContext service is not reachable at 127.0.0.1:PORT. Next: if LifeContext runs as the installed service, check it with `lifecontext service status`: it may still be starting, or recovering after a restart; otherwise start it with `lifecontext serve --port PORT`.
- `api_error`: The service answered with an error this command does not know. Next: update the CLI and restart the service so both run the same version.
- `invalid_api_response`: The service's answer is not what this command expects. Next: update the CLI and restart the service so both run the same version.
- `token_unavailable`: No LifeContext API token for this user: the service writes it when it starts, under the same HOME. Next: if LifeContext runs as the installed service, check it with `lifecontext service status`: it may still be starting, or recovering after a restart; otherwise start it with `lifecontext serve --port PORT`.
- `input_unavailable`: Unable to read the selected source input. Next: give an existing regular file, or - for standard input.
- `unsupported_format`: LifeContext does not import files of that type; the message names the ones it does. Next: save the file in one of those formats and import that copy.
- `vault_missing`: The vault's Journal folder does not exist. Next: create it with mode 0700, or set `vault` in ~/.lifeweave/config.yaml.
- `destination_exists`: The output file already exists; LifeContext never overwrites one. Next: choose a new file name.
- `output_unavailable`: The output file cannot be written. Next: check that its folder exists and that you can write to it.
- `invalid_config`: The config file ~/.lifeweave/config.yaml cannot be read. Next: `lifecontext doctor` says what is wrong.
- `server_start_failed`: The service could not start. Next: `lifecontext preflight` and `lifecontext doctor` say why.
- `nothing_to_change`: Nothing to change. Next: give --provider, --model, --base-url or --api-key-stdin; `lifecontext llm show` shows the current model.

### The MCP server's own codes

- `busy`: A previous MCP tool call is still finishing. Next: report it instead of retrying.
- `path_not_allowed`: The file is outside the import directories the owner allowed, or in a protected place (the LifeContext home, the vault, a hidden directory in the home directory, or a system directory). Next: ask the owner to import it.

### Local commands (`init`, `llm`, `service`, `skills`, `capture`, `backup`, `restore`, `hindsight stop`)

- `setup_failed`: The setup files could not be written. Next: check that ~/.lifeweave belongs to you and is writable, then re-run.
- `backup_failed`: The backup could not be written. Next: check the destination folder, then re-run.
- `restore_failed`: The restore could not read or write its files. Next: check the archive and the LifeContext home, then re-run.
- `hindsight_failed`: The memory engine could not be stopped. Next: `lifecontext doctor` checks the service, the engine and the model.
- `provider_required`: No model provider was chosen. Next: give --provider, or run `lifecontext init` in a terminal to choose one.
- `model_required`: No model was chosen. Next: give --model with the exact ID your provider lists.
- `model_unsupported`: That model does not work with LifeContext yet. Next: choose another model with --model.
- `invalid_llm_settings`: The model settings are not valid. Next: check --provider, --model and --base-url, or `lifecontext llm show`.
- `invalid_api_key`: The API key is not valid: one line of printable ASCII, without quotes. Next: give it again at the hidden prompt or on stdin with --api-key-stdin.
- `api_key_required`: A model API key is required and none was given. Next: give it at the hidden prompt, on stdin with --api-key-stdin, or in the file named by LIFECONTEXT_API_KEY_FILE.
- `api_key_file_unreadable`: The file named by LIFECONTEXT_API_KEY_FILE cannot be read. Next: check that it exists, holds one line and is readable by you.
- `invalid_timezone`: That time zone is not known. Next: give an IANA name such as Europe/Kyiv.
- `invalid_vault`: That vault location is not allowed. Next: choose a folder that is not `/`, not inside ~/.lifeweave and not a parent of it.
- `vault_path_has_symlink`: The vault path in config.yaml goes through a symbolic link. Next: run `lifecontext init --force` to store the real path.
- `unsafe_vault`: The vault folder is not a real folder of yours. Next: choose a folder you own, or fix its owner and type.
- `unsafe_home`: The LifeContext home is not private to you, or HOME is not an absolute path. Next: check that HOME is an absolute path and ~/.lifeweave belongs to you with mode 0700, then re-run.
- `unsafe_credentials`: The credentials file is open to other users or not a regular file. Next: make it a regular file with mode 0600, or delete it and run `lifecontext init` again.
- `invalid_credentials`: The credentials file is malformed. Next: delete it and run `lifecontext init` again to enter the key.
- `config_incomplete`: The config file has no model section. Next: run `lifecontext init --force` with --provider and --model.
- `config_missing`: There is no config file with model settings yet. Next: run `lifecontext init` first.
- `executable_not_found`: The `lifecontext` program to run as a service was not found. Next: install LifeContext so `lifecontext` is on PATH, then re-run.
- `unsupported_platform`: Services are supported on Linux (systemd) and macOS (launchd) only. Next: run the API in a terminal with `lifecontext serve`.
- `unit_write_failed`: The service files could not be written. Next: check that the user service folder is writable, then re-run.
- `unit_remove_failed`: The service files could not be removed. Next: remove them by hand from the user service folder.
- `systemctl_unavailable`: The `systemctl` program was not found. Next: use a systemd session, or run the API with `lifecontext serve`.
- `systemctl_failed`: The `systemctl` program refused the command. Next: run `systemctl --user status lifecontext` to see why.
- `launchctl_unavailable`: The `launchctl` program was not found. Next: run the API in a terminal with `lifecontext serve`.
- `launchctl_failed`: The `launchctl` program refused the command. Next: run `launchctl list` to see the state of the LifeContext jobs.
- `skill_missing`: The agent skill files are missing from this installation. Next: reinstall LifeContext, then re-run.
- `link_failed`: The skill could not be linked into the agent's folder. Next: check that the folder exists and is writable, then re-run.
- `unlink_failed`: The skill link could not be removed. Next: remove the link by hand.
- `capture_requires_inbox`: The capture folder must be named Inbox. Next: leave out --inbox to use Journal/Inbox of the vault, or give a folder named Inbox.
- `capture_unavailable`: The inbox folder cannot be read. Next: check that it exists and that you can read it.
- `unsafe_state`: The capture state in ~/.lifeweave/capture is not private to you or is damaged. Next: check that the folder is yours with mode 0700, or remove its state.json, then re-run.
- `file_exists`: A file is already at that place in the vault. Next: choose a new name.
- `file_too_large`: The file is larger than the vault accepts. Next: use a smaller file.
- `invalid_file`: The vault file is not valid. Next: `lifecontext doctor` checks the service, the engine and the model.
- `not_found`: No such file in the vault. Next: check the name and run it again.
- `read_only_path`: That vault path is read-only for LifeContext. Next: choose another path.
- `unmanaged_file`: The vault file is not one LifeContext manages. Next: leave it as it is, or move it out of the way.
- `sync_conflict`: The vault holds a sync conflict copy of a LifeContext file. Next: settle the conflict in your sync tool, then run `lifecontext reconcile`.
- `export_durability`: The vault copy could not be made durable on disk. Next: check the free space and that the disk is writable, then re-run.
- `locking_unavailable`: File locking is not available on this system. Next: run LifeContext on Linux or macOS.
- `lock_timeout`: Another LifeContext process holds the vault lock. Next: wait a moment and re-run.
- `nothing_to_back_up`: There is no LifeContext data to back up. Next: run `lifecontext init` first.
- `service_running`: The service is running. Next: stop it, or add --stop-service where the command allows it, then re-run.
- `hindsight_running`: The memory engine is still running. Next: run `lifecontext hindsight stop`, then re-run.
- `backup_write_failed`: The archive could not be written. Next: check the free space and the destination, then re-run.
- `catalog_unreadable`: The catalog database is damaged or unreadable. Next: `lifecontext doctor` checks the service, the engine and the model.
- `destination_inside_backup`: The archive would be written inside the data it backs up. Next: choose a destination outside the LifeContext home and the vault.
- `source_changed`: A file changed while it was being backed up. Next: re-run the backup.
- `archive_invalid`: The archive is not a LifeContext backup. Next: check that you gave the right file.
- `archive_unreadable`: The archive cannot be read. Next: check the path and that you can read it.
- `archive_incomplete`: The archive lacks files its manifest lists. Next: make a new backup.
- `archive_changed`: The archive changed while it was being read. Next: copy it to a stable place, then re-run.
- `archive_checksum_mismatch`: The archive does not match its checksum. Next: copy it again or use another backup.
- `archive_config_invalid`: The config file in the archive is not valid. Next: use another backup.
- `archive_instance_invalid`: The memory engine's instance file in the archive is not valid. Next: use another backup.
- `home_not_empty`: This HOME already holds LifeContext data; a restore never overwrites it. Next: restore into a fresh HOME.
- `vault_not_empty`: The vault folder to restore into is not empty. Next: restore into an empty or new folder.
- `vault_outside_home`: The vault to restore is outside this HOME. Next: give `--vault DIR` with a folder inside your home directory.
- `vault_location_refused`: The vault location is not one a restore writes to. Next: give `--vault DIR` with a folder inside ~/LifeContext.
- `schema_unsupported`: The archive was made by a newer LifeContext. Next: update LifeContext, then re-run.

### The memory engine

- `missing_package`: A package of the memory engine is not installed. Next: reinstall LifeContext.
- `unsupported_version`: The memory engine is not the version LifeContext pins. Next: reinstall LifeContext so the packages match.
- `missing_capability`: The memory engine lacks a capability LifeContext needs. Next: reinstall LifeContext so the packages match.
- `missing_credentials`: The model API key is missing. Next: run `lifecontext init` and give the key.
- `document_generation_failed`: The model could not write the document. Next: check the model with `lifecontext doctor --model-probe`, then re-run the command.
- `daemon_unavailable`: The memory engine is not answering or not healthy. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `daemon_port_foreign`: Another program holds the port this HOME's memory engine needs. Next: stop that program, then run `lifecontext doctor`.
- `daemon_port_unavailable`: No free port was found near this HOME's engine port. Next: free a port near it, then run `lifecontext doctor`.
- `daemon_stop_failed`: An old memory engine daemon of this HOME did not stop. Next: stop that process by hand, then run `lifecontext doctor`.
- `engine_auth_disabled`: The memory engine runs without its API token. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `engine_auth_rejected`: The memory engine refused LifeContext's API token. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `engine_token_missing`: The memory engine's token file is missing. Next: start the service once so it writes the token.
- `unsafe_engine_token`: The engine token file is not a private regular file of 64 hex digits. Next: delete it and start again.
- `unsafe_profile_paths`: The memory engine's profile folders are not private to you. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `unsafe_profile_file`: The memory engine's profile file is not a private regular file of yours. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `unsafe_profile_lock`: The profile lock file is not a private regular file of yours. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `profile_lock_unavailable`: The profile lock cannot be taken. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `profile_lock_timeout`: Another LifeContext process holds the profile lock. Next: wait a moment and re-run.
- `profile_setup_failed`: The memory engine's profile could not be set up. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `postgres_setup_failed`: The Postgres of the memory engine could not be set up. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `postgres_binaries_missing`: The Postgres programs of the memory engine are missing. Next: reinstall LifeContext.
- `postgres_start_failed`: The Postgres of the memory engine did not start. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `postgres_stop_failed`: The Postgres of the memory engine did not stop. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `postgres_control_failed`: The Postgres control program failed. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `postgres_state_unknown`: The state of the memory engine's Postgres cannot be told. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `postgres_instance_invalid`: The Postgres instance file of the memory engine is not valid. Next: restore it from a backup.
- `postgres_instance_unsafe`: The Postgres instance files are not private to you. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `postgres_socket_dir_unsafe`: The Postgres socket folder is not private to you. Next: `lifecontext preflight` and `lifecontext doctor` say what to fix.
- `postgres_socket_path_too_long`: The Postgres socket path is too long for this HOME. Next: use a shorter HOME path.
- `postgres_socket_path_unsupported`: The HOME path has a quote, comma or backslash, which the Postgres socket path cannot hold. Next: use a HOME path without them.
- `postgres_instance_missing`: The Postgres data folder has no `instance.json`; pg0 deletes it when one of its own starts fails. Next: restore the file from a backup.

<!-- failure-codes:end -->

## Running the service

Start the API in one terminal; it binds `127.0.0.1` only and stays in the
foreground:

```bash
lifecontext serve
```

In a second terminal, confirm the CLI can reach it:

```bash
lifecontext status --json
```

The service holds an exclusive claim on its HOME, so run one `serve` per HOME.
Commands never start the API implicitly. Stop it with Ctrl-C; readiness should
then fail. The isolated `lifeweave` Hindsight daemon may stay running; this is
expected, and the next `serve` reuses it.

To exercise the provider-backed path by hand, retain a synthetic text under a
new source ID, then look up the returned operation and verify its evidence.
`retain` may incur provider usage:

```bash
printf '%s\n' 'Synthetic LifeContext smoke test. No personal data.' |
  lifecontext retain src_manual_smoke_01 1 --input - \
  --idempotency-key manual-smoke-01 --json
lifecontext operations get "op_<paste-id-from-retain-output>" --json
lifecontext evidence src_manual_smoke_01 --version 1 --json
```

The retained record stays in custody until you forget or delete it with
`curate`; use a fresh source ID and idempotency key for each new manual run.

## Applying changed model settings

`lifecontext llm show` prints the provider, model and base URL and whether a key
is stored, never the key. `lifecontext llm set [--provider P] [--model M]
[--base-url URL] [--api-key-stdin]` changes only those fields (the key from
stdin, or from the file named by `LIFECONTEXT_API_KEY_FILE`), validates as
`init` does and restarts the user service if it runs
(`systemctl --user restart lifecontext.service`, or `launchctl kickstart -k`);
when no service manager is available it prints that command instead. Error
codes are `init`'s plus `config_missing` (run `init` first) and
`nothing_to_change`. If you run `lifecontext serve` in a terminal, make the
change with `llm set`, then follow the steps below.

The `lifeweave` profile keeps the model settings it started with. After you
change the `llm` block or the key, stop the API, then stop only the
`lifeweave` profile, so the next start regenerates its private environment, and
start the API again:

```bash
lifecontext hindsight stop     # refuses with service_running while the API runs
lifecontext serve
```

`hindsight stop` addresses the `lifeweave` profile by name, never the active or
default one, and only when `~/.hindsight/profiles/lifeweave.env` exists in this
HOME; otherwise it reports `not_configured` and changes nothing. A listener on
the profile's port that is not this HOME's daemon is left alone. It then stops
the profile's Postgres (`hindsight-embed-lifeweave`) with that instance's own
`pg_ctl`, also when a daemon died and left it running, and waits for it to
stop. It holds the service lock meanwhile, so `serve` cannot start. Exit 0 when
nothing of the profile runs any more (`state`: `stopped`, or `not_configured`
when the profile was never created here), else 1 with `service_running`,
`daemon_stop_failed`, `postgres_stop_failed` or `hindsight_running`.

Never stop another Hindsight profile (for example one used by a different
agent) for LifeContext work.

HTTP health alone does not prove the model route works: `lifecontext preflight
--model-probe` checks it in a minute, and `preflight --retain-smoke` or a
synthetic retain takes the whole retain path.

## Running as a user service

```bash
lifecontext service install --maintenance --dry-run   # print the files and commands
lifecontext service install --maintenance             # write, enable and start
lifecontext service install --maintenance --capture   # also take in notes from Journal/Inbox every 30 s
lifecontext service install --no-start                # write and enable for the next login only
lifecontext service status                            # units and the last maintenance run
lifecontext service uninstall --maintenance           # remove only the maintenance timer
lifecontext service uninstall --capture               # remove only the capture timer
lifecontext service uninstall                         # remove everything it installed
```

On Linux, `install` writes `systemd --user` units to
`~/.config/systemd/user/` (or `$XDG_CONFIG_HOME/systemd/user/`):
`lifecontext.service` runs `lifecontext serve --port PORT` with the absolute path
of the `lifecontext` next to the running Python (the install's virtual
environment), restarts it on failure after 10 s, and puts that directory first
on `PATH` so Tesseract and Hindsight are found. The API, its Hindsight daemon and
Postgres share the unit's control group, which gets `MemoryHigh=4G` (past it the
kernel reclaims and slows the group instead of starving the desktop; there is
no hard cap) and `Nice=5`. The units go to the account's home from the password
database, where the service manager reads them; when `HOME` points elsewhere
for the data, the units carry `Environment=HOME=` with that directory. With
`--maintenance` it adds
`lifecontext-maintain.service` (`lifecontext maintain --json`, a oneshot with
`MemoryMax=512M`, `CPUQuota=50%`, `Nice=10`) and `lifecontext-maintain.timer`
(daily at 07:00 local time, `Persistent=true`, so a missed run happens at the
next boot). It then runs `systemctl --user daemon-reload`, `try-restart` when
the service unit changed, and `enable --now` for the service (and the timer).
With `--capture` it adds `lifecontext-capture.service` (`lifecontext capture
--once --json`, a oneshot with the same limits as maintenance, after the API
unit) and `lifecontext-capture.timer` (`OnActiveSec=30s`, `OnUnitActiveSec=30s`,
`AccuracySec=1s`, so runs are not merged into one per minute); on macOS the agent
`io.github.rundax.lifecontext.capture.plist` has `StartInterval` 30. What
`capture` does is in [sync](sync.md#2-capture-notes-from-the-inbox).
Re-running with the same options changes nothing. `--no-start` writes the units
and runs `daemon-reload` and `enable` without `--now`: nothing starts or
restarts until the next login (on macOS the agents load at the next login). A user service stops when you
log out unless lingering is on: `loginctl enable-linger "$USER"`. Logs are in
`journalctl --user -u lifecontext`. Stopping the service also stops the
Hindsight daemon and Postgres it started, since they run in its control group.

On macOS, `install` writes launch agents to `~/Library/LaunchAgents/`:
`io.github.rundax.lifecontext.plist` (run at load, restarted when it exits with
an error, `Nice` 5) and, with `--maintenance`, `io.github.rundax.lifecontext.maintain.plist`
(daily at 07:00 with `Nice` 10, background process type and low-priority I/O;
launchd has no memory or CPU caps). It loads changed agents with `launchctl
bootout` and `bootstrap gui/UID`, and bootstraps an unchanged agent that
`launchctl print` does not find, so a re-run recovers from a first load that
failed. Logs go to `~/Library/Logs/lifecontext/`.

`uninstall` disables and removes only these files; other units, such as a
development checkout's `life-context-dev-*` or `lifeweave-dev-livesync`, are
never touched. When `systemctl` or `launchctl` fails, for example without a user
session bus over ssh or in a container, the files are still removed and the
report lists each failed command under `warnings`. `--port` selects the API port
written into the units. Windows is not supported.

Exit codes for `service`: 0 on success (`uninstall` also with warnings), 1 with
`{"error": CODE}` under `--json`: `systemctl_failed`, `systemctl_unavailable`,
`launchctl_failed`, `unit_write_failed`, `unit_remove_failed`,
`executable_not_found`, `unsupported_platform`.

## Daily maintenance

```bash
lifecontext maintain --json                      # what the timer runs
lifecontext maintain --date 2026-10-17 --json    # treat that day as today
lifecontext maintain --retry-wiki 3 --rebuild-stale 2 --json   # opt-in model calls
```

`maintain` uses the running service. It runs `reconcile` (up to 4096 inventory
rows), `wiki lint`, and writes the brief for yesterday and today
(`wiki/Briefs/DATE - Brief.md`) plus `wiki/Log.md`, with "today" in the
configured `timezone`. That makes no model calls. `--retry-wiki N` rebuilds the
wiki of at most N documents whose wiki stage failed, and `--rebuild-stale N` at
most N documents behind stale source pages; both call the LLM. Their idempotency
keys are `maintain-DATE-retry-wiki-SOURCE` and `maintain-DATE-rebuild-stale-SOURCE`,
so a second run on the same day replays instead of paying again. The JSON report
holds counts and states only. Each run records its start, end, date, outcome and
counts in `~/.lifeweave/maintenance.json`, which `service status` shows; the
exit code is 1 when a step failed.

## Backup and restore

```bash
lifecontext backup                          # ~/LifeContext/backups/lifecontext-backup-UTC.tar.gz
lifecontext backup /mnt/usb/                # into a directory, or to a file name
lifecontext backup --stop-service           # stop the service and the profile, back up, start it again
lifecontext restore ARCHIVE                 # into a HOME without LifeContext state
lifecontext restore ARCHIVE --vault ~/Vault # put the vault elsewhere
```

Stop the service first (`systemctl --user stop lifecontext`, or Ctrl-C on
`serve`), or let `--stop-service` do it. `backup` refuses with `service_running`
while the API answers on `--port` or holds `~/.lifeweave/service.lock`, and with
`hindsight_running` while the `lifeweave` profile's Postgres runs (after a
foreground `serve`, run `lifecontext hindsight stop`, see
[Applying changed model settings](#applying-changed-model-settings)). It holds
the service lock while it reads, so the service cannot start meanwhile.

`--stop-service` checks the destination first and notes whether the user
service runs; then it stops it (`systemctl --user stop lifecontext.service`, or
`launchctl bootout` of the agent), stops the `lifeweave` Hindsight profile as
`hindsight stop` does (never another profile), waits up to 30 s each for the API
and Postgres to go away, takes the backup and starts the service again if it
was running, also when stopping it or the backup failed or was interrupted. A foreground `serve` is not
stopped (`service_running`); stop it yourself. The report adds `stopped`
(`service`: the units stopped, `hindsight`: `stopped` or `not_configured`),
`restarted` and `warnings`. If the service does not start again, the archive is
still written, `warnings` holds `service_restart_failed` and the exit code is 1.

The archive (mode `0600`, with a `.sha256` file next to it) contains
`~/.lifeweave` (custody archive, catalog with its operations, derived store,
wiki revisions, vision cache, `config.yaml`), the Hindsight profile's embedded
Postgres instance `~/.pg0/instances/hindsight-embed-lifeweave`, and the vault,
plus `manifest.json` listing every entry with its mode, size and SHA-256. The
Postgres instance includes `instance.json`, which holds the password of that
local database, so keep archives as private as the data in them; the restored
database opens with that password. It leaves out `.env` (keep your key
elsewhere), `api.token` and `hindsight-api.token` (the next start makes a new
engine token), locks and caches, the Postgres socket directory
`~/.lifeweave/postgres`, the generated Hindsight profile file (it holds the key
and is regenerated), the Postgres binaries (the first start after a restore
unpacks them again through a throwaway pg0 instance) and the model cache, and
symbolic links. The destination may not lie
inside what it backs up. The manifest records HOME and the vault by their real
paths (and HOME as given when a link leads to it), so a HOME reached through a
link restores like any other. Where the destination has no hard links (FAT drives,
some network shares), the finished archive is copied into place instead, still
without replacing an existing file.

`restore` works only when the current HOME has no `~/.lifeweave` and no
`hindsight-embed-lifeweave` instance, and the vault location holds nothing or
an empty directory you own (a volume mount point, as in the Docker image). It
first reads the whole archive and checks the `.sha256` file (when present) and
every entry against the manifest, writing nothing; then extracts into a staging
directory in HOME, checks that the catalog opens and its schema version is
supported, moves the vault, the Postgres instance and `~/.lifeweave` into place
(rolling back the moved ones on error), sets `~/.lifeweave`, `~/.pg0`, the
instance and the vault root to mode `0700`, and deletes the staging directory.
Whatever the archive says, vault folders get mode `0700` and vault files `0600`,
so nothing restored into the vault can be executed.
An existing empty vault directory, or one on another filesystem, is filled
entry by entry (hard links where possible, else copies checked against the
manifest's SHA-256), and a failure leaves it empty again. A vault that was
inside the old HOME goes to the same place in the new one, and `config.yaml`
(with the vault's real path) and the Postgres `instance.json` are rewritten for
the new paths. Afterwards,
add the key with `lifecontext init`, run `lifecontext preflight` and
`lifecontext doctor`.

Archives are trusted input: restore only archives you made. `restore` still
keeps every write inside the new HOME or the `--vault` you give. It refuses
members that are links, devices, absolute or contain `..`, anything outside
`home/.lifeweave`, `home/.pg0/instances/hindsight-embed-lifeweave` and `vault`,
and `.env`, `api.token`, `hindsight-api.token` and the lock files in any
letter case (`archive_invalid`). Without `--vault`, the vault location from the manifest,
with links resolved, must lie inside the new HOME (`vault_outside_home`) and
below `~/LifeContext`, the default vault's parent (`vault_location_refused`):
elsewhere in HOME, such as `~/bin`, `~/.local/bin`, `~/.config` or
`~/Library`, restored files could be run. Pass `--vault DIR` for anything else,
such as a vault that lived elsewhere or outside the old HOME. What it
restores still acts, though: `config.yaml` chooses where model requests go, and
the Postgres instance (`instance.json`, `postgresql.auto.conf`) can run
commands when Postgres starts.

Error codes, with exit code 1: `nothing_to_back_up`, `service_running`,
`hindsight_running`, `destination_exists`, `destination_inside_backup`,
`backup_write_failed`, `source_changed`, `invalid_config`, `catalog_unreadable`
for `backup`; `archive_unreadable`, `archive_invalid`, `archive_incomplete`,
`archive_checksum_mismatch`, `archive_changed`, `archive_config_invalid`,
`archive_instance_invalid`, `home_not_empty`, `vault_not_empty`, `unsafe_vault`,
`invalid_vault`, `vault_outside_home`, `vault_location_refused`,
`schema_unsupported`, `service_running`, `restore_failed` for `restore`.

## Agent access

Agents reach LifeContext only through the CLI, guided by the
`personal-journal-operations` skill. The skill's operator boundaries apply: on
any failure the agent stops and reports the command and output; the owner fixes
the code, config or service. Installing the skill and the agent rules are in
[agents.md](agents.md).

## Recall behavior

`recall` returns only results that map to exactly one locally held source version.
Hindsight consolidates retained facts into observations per tag set, so each retain
carries the tag `src:<source_id>:v<version>`; without it every source shares one scope
and similar facts from different sources merge into an observation that no single
source can back. The tag is a required argument of the retain adapter.

- A result that cannot be attributed is never shown. It is omitted and counted in
  `omitted_unresolvable`. If nothing citable remains, `recall` fails with
  `evidence_unavailable`. `reflect` stays strict because an answer must be fully attributed.
- Journal sources indexed before per-source tags may still sit in merged observations.
  Run `scripts/check_observation_scopes.py --base-url URL` (read-only; exit 1 when a
  merged observation exists), where `URL` is the `base_url` that `lifecontext preflight`
  prints for the `lifeweave` profile; the script's default, `http://127.0.0.1:19007`,
  may not match it. The script sends the engine token from
  `~/.lifeweave/hindsight-api.token` (`--token-file` names another). Then run `curate reindex SOURCE_ID` for each source it lists, which
  re-tags that source's facts, and the same script with `--apply`, which resets the facts
  behind merged observations so Hindsight rebuilds them per source. Back up the Hindsight
  database first; observations are derived data and are regenerated by the LLM.
- Reranking is CPU-bound. The profile environment (`_RERANKER_ENV` in
  `src/lifecontext/hindsight.py`, written to the profile env file on every start) selects
  the local multilingual model `nreimers/mmarco-mMiniLMv2-L6-H384-v1` (Ukrainian, Russian,
  English; about 450 MB cached under `~/.cache/huggingface`) and limits
  reranking to 100 candidates, length bucketing and two concurrent jobs. A different model
  needs no re-indexing, only the restart below. On the maintainer's 4-vCPU development
  host a recall takes about 20-40 s when the host is quiet and more under load; your
  timing depends on your CPU. Recall work cannot be cancelled once started, so do
  not retry a slow recall with a short client timeout: give it at least 300 seconds and
  send one at a time. Apply a change to these limits by stopping the API, stopping only
  the `lifeweave` profile (see [Applying changed model settings](#applying-changed-model-settings))
  and starting the API again.

## Projection verification and recovery

```bash
lifecontext reconcile --json
```

Reconciliation checks actual files against exact custody; it recreates missing
managed projections, preserves edits as durable conflicts, and never resubmits
sources to the provider. Review the `failed` count even when the HTTP request
succeeds. The complete inventory must fit `--limit` (default 200, maximum 4096);
a larger inventory fails closed before writes. Increase the limit explicitly
when needed.

A failed retain keeps exact custody and a durable failed operation, and its
journal entry is already in the vault. Inspect
`operations get <operation-id> --json`. Repeating the same idempotency key
replays a completed or permanently failed result without a new submission.
After a retryable provider failure (`retain_failed` or `provider_unavailable`),
the same key with the same input runs a journal retain, correction or reindex
again from custody, but only after a delay: as long as the operation had been
failing at its last refusal, at least 1 minute and at most 6 hours, so it
roughly doubles each time. A retry before then gets the same retryable error
and the provider is not called;
`lifecontext capture` repeats pending notes this way. Each start also
resubmits up to five refused journal notes from custody and stops at the first
one the provider refuses again. A deleted or forgotten note is never
resubmitted. `lifecontext status --json` reports `indexing_pending`: how many
current journal entries are not indexed yet (pending or failed) and up to 20 of
their vault-relative paths (plain `lifecontext status` prints both on one
`Journal entries waiting for indexing:` line); `doctor` and the MCP `status` tool show only the
count. For a known provider
failure you can also run `curate reindex <source-id> --expected-version <N>`
with an explicit new idempotency key after repairing the route. For projection
failure, use reconciliation after resolving permissions or reviewing the edit;
never edit the catalog or overwrite conflicting Markdown. An entry edited
before its indexing finished keeps the edit and is reported as a projection
conflict. Restart recovery uses exact provider evidence and managed hashes;
outcomes it cannot check, because the provider cannot be asked, remain
unresolved rather than being blindly resubmitted.

## Journal and wiki file names

Every journal entry file is named `YYYY-MM-DD - Title.md`, with numbered
collision suffixes and later version suffixes, so file-name order is date
order. In Obsidian, sort the file list by "File name (Z to A)" to see the newest
entries first. The date is the one the source states: a provenance date, a line
among its first three that only states a date, such as
`Entry date: 2026-10-12 (Europe/Vienna).` or `Mon, Sep 14, 2026`, or a dated
heading. A source that states no date is named by the day it was retained; a
date merely mentioned in its opening sentence is used instead when it falls in
the month before that day or on the day after it. The retained day is read in
the top-level `timezone` setting of `config.yaml` (see
[Vault, time zone and limits](#vault-time-zone-and-limits)). It is the day this home first
stored the source, so after a bundle import it is the import day. The title is
the provenance title, the first heading, or the opening sentence; lines that
only carry the date, timezone or provenance are not used as titles.

Source IDs and checksums stay in frontmatter and the catalog; they are not
filenames. Reconciliation also moves unchanged entries that still carry an
earlier name (legacy ID names and names without a date), refreshes managed
relationship links without changing decisions, and keeps the legacy ID stem as
an Obsidian alias. Edited notes and destination conflicts are left untouched.
Interrupted moves resume during recovery.
Existing forgotten projections can be renamed without changing retrieval
visibility or resubmitting content; missing forgotten/deleted entries are not
recreated by this migration.

User-authored notes are never rewritten. Bare legacy wikilinks can resolve via
aliases; literal Markdown paths and explicitly rooted wikilinks may need manual
updates after migration. Reconciliation reports `legacy_path_references` when
configured relationship roots contain affected references (`-1` means the
bounded audit could not finish). Check those notes before synchronizing.

LLM wiki projections likewise use readable titles for current pages and
`Title - rN.md` for history snapshots. Wiki IDs, revisions, checksums, and
legacy-name aliases stay in frontmatter and the catalog. The wiki index and
history links use the recorded paths with URL-encoded filenames, including
Unicode and spaces, for navigation on desktop and phone.

In the vault, a current page's front matter also carries `state: current`,
`stale` or `conflict`; a stale page opens with the callout `> [!warning] Stale:
a cited source changed; rebuild pending`. Each contradiction is a `> [!note]
Contradiction (left date / right date)` callout with both quotes and their
citations. A citation links to the projected source note of that document
version, at the `Page N`/section heading above the cited passage when the note
has one, and is labelled `Title, p. 3` (slide or sheet where that is the unit,
`v2` only when the document has several versions, a short source ID such as
`(doc_1a2b)` when two documents on the page share a title; Word citations carry the
title alone). `wiki show --json` returns the same labels in its `managed` text and a
`citations` list with each citation's source ID, version, extraction revision and
passages, so an agent can still identify the source; a document page shows under
its title how many of the document's pages the claims come from and which were
not read; `Related` pages link to their projected files and show a plain title
until the target is projected. A state change or a newly projected link target
re-renders the file on the next projection or `reconcile` without a new wiki
revision; user sections keep their bytes, and the front matter's
`managed_checksum` must still match the managed block, so a hand edit of either
remains a conflict. `wiki show` and the API return the same text without state
and links.

`reconcile` migrates existing wiki projections locally, without rerunning the
LLM or changing stored claims, citations, or revision artifacts. User-section
bytes are preserved; edits to generated metadata/content and conflicting
files block the affected migration. Interrupted publication/moves resume
using the existing catalog and inode proofs. `wiki/SCHEMA.md`, source-document
filenames, and original attachments are unchanged. User-authored literal links
to old wiki paths may need manual updates; bare wiki-ID links use aliases.

## Document OCR and standalone wiki refresh

OCR requires the native `tesseract-ocr` executable (on Debian/Ubuntu,
`sudo apt-get install tesseract-ocr`; on Arch Linux, `sudo pacman -S --needed
tesseract tesseract-data-eng tesseract-data-rus tesseract-data-ukr`). The Python dependencies already include
image and PDF rendering support. Check `tesseract --version` and
`tesseract --list-langs`; the current extraction command uses Tesseract's
installed English/Russian/Ukrainian language data (`tesseract-ocr-rus` and
`tesseract-ocr-ukr` provide the latter two). OCR text with Latin and Cyrillic
look-alikes inside words, or with words that alternate scripts in a PDF whose
text layer up to that page is almost all Latin, is kept but flagged
`ocr_text_suspect`. Restart
`lifecontext serve` after installation because executable availability is
discovered at startup.
Native PDF text is preferred; blank-text PDF pages and images use local OCR.

Use `lifecontext wiki rebuild --source-id SOURCE_ID --version VERSION
--idempotency-key UNIQUE_KEY --json` to refresh indexed evidence without
reingestion. Omit version for the current source version; omit both selectors
for active, current, indexed documents. Bulk rebuild rejects more than 100
eligible documents or an inventory reaching its 4096-row scan ceiling.
Rebuild projects the resulting wiki revisions while preserving user sections.

Check operation state even on HTTP success: admitted provider/projection
failures expose durable operation IDs and partial publication counts. Reusing
a key replays saved status and never repeats generation. Use a new key for an
intentional retry after diagnosing a provider failure; use reconciliation for
projection failures. A crash can leave an operation running; inspect its
sources/pages before initiating another rebuild.

PDF extraction is bounded at 5000 pages and 600 seconds, with existing byte,
text, pixel, and memory ceilings retained. Empty OCR output on an individual
PDF page preserves other pages and emits `ocr_no_text_page:N`; an entirely
textless PDF still fails. Document API responses expose the latest
`extraction_warnings`. Successful extraction is not a claim that graphics or
unresolved pages were completely read. Retry the saved document operation with
`--rerun-extraction` after fixing extraction; keep its source ID/version and
original checksum, rather than importing a duplicate or a summary.

Synchronous Hindsight requests have a bounded 1800-second client timeout to
allow book-length retains; the CLI waits 5 seconds longer than that. Document
import/retry CLI requests add the 600-second extraction budget and up to 1,865
seconds for vision reading (1,800) and its capability probe (65), so a single
import can wait about 71 minutes in total. A timed-out retain may still run remotely; do
not restart/retry while it is processing or infer full completion from the
first indexed memory units. Wait for terminal remote completion before
recovering the existing operation. Other stalled Hindsight calls may also
wait up to 1800 seconds.

Reads (every `GET`: `status`, `documents list`, `wiki show` and the like) time out
after 30 seconds; the calls above keep their own, longer timeouts. The service
extracts the text of an import (OCR on a scanned PDF can take minutes) before it
answers any other request, so a read sent during an import can time out even though
nothing is wrong: the CLI then says the service may be busy with an import and to
try again in a few minutes.

Managed wiki responses may be a single JSON object/envelope or one standalone
JSON Markdown fence. Prose wrappers, duplicate keys, invalid schemas, and
unsupported citations remain rejected. Each document compiles into its own page
(`source_<source id>`, titled like the document); recompiling a document replaces
that page's claims instead of adding to them. A page holds at most 48 claims from
one document (8 on shared topic and entity pages) under the 48 KB artifact
ceiling: a longer document's evidence windows are sampled evenly from its first
to its last page, and a page that fills up early stops asking the model rather
than failing. Each evidence window requests up to one short claim per 3 KB of
its text (1 to 8, never more than its share of the 48) for the document's own
page; windows never create other pages. Each claim is checked on its own: a window
keeps its valid claims when others fail their quote check, the dropped ones are
logged as counts and an error code (never the text), and only a window with no valid
claim is rejected and asked again once. A quote is found in its cited passages byte
for byte, else once both are normalised (Unicode NFKC, any run of whitespace as one
space, every quotation mark as `'` or `"`, every dash as `-`, no soft hyphens or
other format characters, no hyphen breaking a word across lines), else, for a quote
of at least 20 normalised characters, as the one span of those passages whose
character trigrams are at least 85% alike (Dice) with its characters in the same
order (85% by `difflib`) and no other span within 5 points of it; the near search
covers passages of up to 60,000 characters and 32 quotes per answer. The page stores
the exact text of that span as the quote and the matching part of it as the claim,
so a published claim and quote are always literal text of the extraction, and a
paraphrase or reordered sentence is still rejected. Dropped claims are kept, with
their reason (`quote_not_found`, `claim_not_in_quote` or `invalid_citation`), in
`wiki-windows.json` next to the version's extractions under
`~/.lifeweave/derived/`; a forget or delete removes the file, and `documents show`
gives only their number (`wiki_dropped_claims`). The same file keeps each checked
window, keyed by the version's checksum and extraction revision, a hash of the
window's evidence, the claims asked for, the model and the compile instructions: a
retry or `wiki rebuild` checks the kept claims again without the model and asks only
for windows that are missing or whose claims no longer check out (without a model
name, as with Hindsight reflect, nothing is reused). A window that still fails after
the call's retries is skipped and the page publishes from the others, with the wiki
stage `completed` and `wiki_windows_skipped` in `problems` until a retry fills them;
after two windows in a row fail at the provider, the rest are skipped without
asking. Only when no window validates does the stage fail, with the first window's
code. After the summary below, one
routing request places the own page's checked claims on shared pages: the model
sees the claims by number, a catalog of up to 300 existing `topic_`/`entity_`
pages (12 KB, pages that already cite the document first, then by words shared
with the claims) and the claims of the best-ranked pages, and returns at most 6
routes of at most 8 claim numbers, each to an existing page or to a new topic
or entity title. It writes no quotes: the routed claims are the own page's
validated claims. A new title gets a deterministic ID (`topic_<ascii slug>`, or
`topic_<12 hex digits of SHA-1>` of the NFKC-casefolded title when it has
non-Latin letters); when that page exists, the claims are added to it. A route
may name an existing claim that a routed claim contradicts; the contradiction is
published with dates taken from the two documents (provenance `captured_at`,
else the day the document was retained), never from the model, and opens a
review, unless the owner already resolved the same contradiction. A shared page
keeps other documents' current claims, replaces this document's (at most 8 per
document, 128 claims and 48 KB per page; at either page bound this document's
claims are left out first) and links to every contributing document page; the
own page links to the shared pages that hold its claims. A routed contradiction
must name one of its route's claims. A rebuild routes again, and pages no
longer routed lose the document's claims (a page that would be left empty keeps
its revision). Each shared page is
published with a revision check; if another compile changed it meanwhile, it is
reloaded and merged once without a model call, and a second conflict (a
hand-edited page) leaves it for the next compile, as does a page whose other
documents' evidence no longer checks out. At most 6 new routes count toward
the 16-page limit of a compile; pages that only lose the document's claims do
not. A malformed routing response may be regenerated once; a failed routing
leaves the shared pages and the own page's links as they were, publishes the own
page and shows as `source_unrouted` in `wiki lint` (also when no shared page
holds claims of the document's current version). A shared page that no document
routes to any more keeps its last revision but shows "Retired: no document
routes claims here anymore." A rebuild that changes nothing on a shared page
publishes no new revision of it.

Forgotten and deleted documents never appear in the wiki: every rendering, of
current and earlier revisions, leaves out their claims, citations,
contradictions, summary and links to their pages. This is decided per document
version, so a forgotten version stays out where a later one is live. A page
that cites nothing else shows "Withdrawn: this page's sources were forgotten."
under the title "Withdrawn page" (also in `wiki list`, `wiki status`, `wiki
show` and `indices/wiki.md`), and its file and history files are renamed to
"Withdrawn page" (numbered when taken). Their claims are never sent to the
model in later routing requests. A contradiction review that quotes them is no
longer listed by `wiki reviews`, counted in the brief or reported by `wiki
lint`, and cannot be resolved; it is never resolved for you either. Briefs
and `wiki/Log.md` generated after the forget show no title of theirs, nor of a
page that cites only them (after a forget earlier brief files are left as
written; a delete writes again the brief files that name or quote the
document, and the log), and `wiki lint` does not report withdrawn pages as
stale. A page keeps its ID, and a shared page's ID is made from its title, so
a withdrawn `topic_` or `entity_` page still names its subject in its ID (in
`wiki list`, briefs and front matter). A forget or delete also republishes,
without a model, the shared pages that held the document's
claims and the index, and at once rewrites every vault page and history file
that ever cited the document, without new revisions; user sections keep their
bytes and move with a renamed file, and a file whose managed text was edited by
hand is left alone as a projection conflict (an edited history file does not
keep its page from being rewritten). A delete also erases the document's
text from storage: its extracted text (`~/.lifeweave/derived/<source_id>`) is
removed, and in every stored revision of every page that cited it
(`~/.lifeweave/wiki_revisions`, history of shared pages included) its
citations keep their place and passage but quote `[erased]`, and a claim
whose text only it held reads `[erased]` too, with the summary sentences that
rested on such a claim; contradiction reviews are rewritten the same way. A
changed revision gets a new artifact under its new checksum and the catalog
points to it, with the same parents and dependencies; the old file is removed.
The document's own wiki page takes the title "Withdrawn page" in the catalog
and in its artifacts (its page ID stays). Nothing else is touched, and what the
vault shows does not change; the document's title and file name stay in the
catalog's document record and in operation receipts. The step runs before the
delete counts as complete, so a failure leaves `custody_delete` running for the
retry, and it can be repeated safely. A stored revision file that is missing is
logged and skipped, and one that fails its checksum or is not a revision is
logged and removed, so neither holds a delete back; any other failure to read
one leaves the delete unfinished for the retry. The step
ends by giving the document's own page the withdrawn title; `reconcile` (the
daily `maintain` runs it) does the step again for every deleted document whose
page has not that title yet or whose extracted text is still there, which also
cleans up documents deleted by an earlier version, and leaves the others alone.
A forget changes none of this: it hides and keeps. A forgotten or deleted document
takes no new version: importing a replacement fails with `invalid_request`;
import the file as a new document instead. A forgotten document is not indexed
again either: `curate reindex` fails with `conflict`, and recall never cites a
forgotten version. After the evidence windows, one more request asks for a summary of the
document page: up to five sentences of at most 300 characters, each naming the 1
to 4 numbered claims it rests on, with no links or markup. Claims are numbered
and the summary sits above them under the label "Model summary, generated from
the numbered claims below; verify before relying on it."; it is not checked
against the quotes (`wiki status` reports `summary_model_generated: true` next to
`semantic_support_verified: false`). A rejected or failed summary never fails the
document; the page publishes without one and `wiki lint` reports
`summary_missing`. The `index` page is built without a model after each compile:
one entry per document page, in title order, linked to that page, for as many
pages as fit in 128 links and 48 KB, then "And N more: `lifecontext documents
list`". An entry shows the page's first summary
sentence (labelled) or, without a summary, its first current claim that is not
copyright, ISBN or publisher imprint boilerplate. An index problem never fails a document,
and an index whose managed text was edited by hand is left alone until resolved. The HTTP service compiles wiki patches and category suggestions directly
with the pinned LLM provider using the same validated model, URL, reasoning
effort, and private credential file. Compilation supplies bounded evidence
and no tools; Hindsight recall/reflect remains available for retrieval. Each
compile call has a 180-second deadline per attempt and closes its provider client;
a timeout, rate limit (429), server error (5xx) or lost connection is asked again
up to three times, after 2, 4 and 8 seconds or the provider's `Retry-After` up to
120 seconds (a longer `Retry-After` ends the retries), and any other failure is not
retried. The service imports the provider client in a background thread when it
starts, so the first compile does not spend its deadline on the import. A response
rejected by schema/citation validation may be regenerated once for that window after
rechecking the source; a second rejection skips that window. OpenAI chat-compatible compilation requests
JSON-object mode, which the selected endpoint must support. Schema and literal-citation validation still gate publication.
