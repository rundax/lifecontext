# LifeContext operations

Configuration, startup checks, recovery and document handling for a local
LifeContext. Commands below use the installed `lifecontext` command, which
talks to the API on loopback port 8765 by default (`--port` selects another).
In the development checkout use `bin/lifecontext-dev` instead, which pins port
18765; see [development.md](development.md). How the parts fit together is in
[architecture.md](architecture.md); what leaves the machine is in
[privacy.md](privacy.md).

## Setting up with `lifecontext init`

```bash
lifecontext init                     # asks; the key is read with a hidden prompt
lifecontext init --non-interactive --api-key-stdin \
  --provider anthropic --model claude-sonnet-5 --timezone Europe/Vienna < key.txt
```

`init` creates `~/.lifeweave` (mode `0700`), writes `config.yaml` with the `llm`,
`vault` and `timezone` blocks described below and `.env` with
`LIFEWEAVE_LLM_API_KEY` (both mode `0600`), and creates the vault root, its
`Journal` directory and `Journal/entries` (mode `0700`; an existing directory you
own is tightened to `0700`). Defaults: provider `anthropic`, model
`claude-sonnet-5` (`claude-haiku-5-5` costs less), vault `~/LifeContext/vault`,
and the system time zone (from `TZ`, `/etc/timezone` or the `/etc/localtime`
link, else `UTC`). Other flags: `--base-url`, `--vault DIR`,
`--non-interactive`, `--json`.

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
  `{"error": CODE}` under `--json`, 2 for a usage error.
- A loopback `openai_compatible` endpoint needs no key (see [API key](#api-key)).
- Claude Sonnet 5.5 and Opus 5.5 are reported with a warning: they reject the
  forced tool call that Hindsight 0.9.2 uses for fact extraction, so retain fails.
- It never starts the API or Hindsight; it prints the next steps.

## Diagnostics with `lifecontext doctor`

```bash
lifecontext doctor           # readable summary
lifecontext doctor --json    # the full report
```

`doctor` only reads. It reports the LifeContext, platform and Python versions;
the pinned Hindsight package versions; `command -v lifecontext` (the absolute
path to put in MCP client configs); the paths and modes of `~/.lifeweave`,
`config.yaml`, `.env`, the vault root and `Journal`; a config summary (provider,
model, base URL, time zone, which limits are customised) and whether the keys are
present, never their values; the service on `--port` (`/healthz`, then
`/v1/status` and the wiki lint counts with the local token); unfinished and
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
journal, document or wiki content, and no key, token or password.

It exits 1 only for broken prerequisites, listed in `problems`:
`python_unsupported`, `hindsight_packages`, `home_missing`, `home_unsafe`,
`config_missing`, `config_invalid`, `credentials_missing`, `credentials_unsafe`,
`credentials_invalid`, `vault_missing`, `vault_unsafe`, `vision_blocked`,
`engine_auth_off` (the daemon does not require the token; restart the service,
see [Engine access](#engine-access-token-port-and-postgres)), `engine_port_taken`
(something else listens on the profile's port), `postgres_tcp` (Postgres
listens on TCP or the profile is set up for it), `postgres_files_unsafe` (the
socket directory is not `0700` or not `~/.lifeweave/postgres`, or
`instance.json` is not a private file of yours), `engine_profile_unsafe`.
`warnings` do not change the exit code: `service_not_running`,
`service_not_ready`, `tesseract_missing`, `tesseract_languages_missing`,
`models_not_cached` (normal before the first start), `disk_low` (under 2 GB).

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
```

`provider` and `model` are required. `base_url` is required for
`openai_compatible` and `volcano`; it is optional for other native providers
that have a built-in endpoint. Use the exact lowercase provider ID:

- `openai_compatible` — any service that implements OpenAI Chat Completions.
  LifeContext maps this to Hindsight's `openai` adapter. Supply that service's API root
  in `base_url` (usually ending `/v1`), not its `/chat/completions` endpoint.
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

LifeContext rejects unknown provider IDs, unknown fields inside `llm`, old
`hindsight.llm_provider`/`llm_model`/`llm_base_url` fields, invalid URLs, and
credentials in YAML.

Remote URLs must use HTTPS. HTTP is allowed only for loopback (`localhost`,
`127.0.0.0/8`, or `::1`). URLs cannot contain credentials, query strings, or
fragments. Never put a key in `base_url`.

## API key

Put the API key matching the selected provider in the private file
`~/.lifeweave/.env`:

```dotenv
LIFEWEAVE_LLM_API_KEY=<your provider API key>
```

`.env` holds at most two keys: `LIFEWEAVE_LLM_API_KEY` for the `llm` provider
and, optionally, `LIFEWEAVE_VISION_API_KEY` for the vision model (see below).
LifeContext requires a non-empty LLM key for every provider except
`openai_compatible` with a loopback `base_url` (`localhost`, `127.0.0.0/8` or
`::1`): a local Ollama, llama.cpp or vLLM server needs none, `.env` may then be
absent, and Hindsight gets the placeholder key `local`. A key in `.env` is still
used when present. It does not read provider-specific variables, Hermes
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
stays on the machine. `base_url` follows the same rules as for `llm`; `gemini`
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
unreachable, the import fails retryably; `documents retry OPERATION_ID` resumes
the same extraction revision from the cache. Changing the model never rewrites
existing documents; `documents retry OPERATION_ID --rerun-extraction` re-reads
them, and only the routed pages call the new model.

An item the model refuses, or answers twice with unusable JSON, stays unresolved
(`vision_refused:...`, `vision_failed:...`) and the document completes without it.
A plain retry does not ask again. `documents retry OPERATION_ID
--rerun-extraction` makes a new extraction revision with its own re-index: it
asks again for `vision_failed` items, while a refusal stays cached and is not
asked again until the vision provider or model changes.

On start, the service resumes unfinished imports, including their vision reads,
before it answers requests, as it already does for unfinished indexing. After a
long outage with several pending documents, startup can take minutes per
document; the service's log shows that it is busy, not hung. Under
`lifecontext service install` the log is in `journalctl --user -u lifecontext`
on Linux and `~/Library/Logs/lifecontext/` on macOS (see
[Running as a user service](#running-as-a-user-service)).

`documents import --no-vision` keeps a document away from vision for good,
including its later versions; the choice is recorded in its extraction ledger
and honoured by recovery and every rerun. `documents retry ... --rerun-extraction
--vision` lifts it.

## Startup checks

```bash
lifecontext preflight
lifecontext preflight --retain-smoke
```

Both commands use only the `lifeweave` Hindsight profile, bound to
`127.0.0.1`. They may start the profile and leave it running after the check;
an already-running profile is reused without reconfiguration. Restart the
profile before applying changed model settings. The JSON field `daemon_status`
reports `running`, `engine_auth` reports `enforced` and `postgres` reports
`socket_only`. The second command also retains one fixed disposable test
source in the local Hindsight database. Both emit redacted JSON. They do not
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

Engine access adds: `unsafe_engine_token` (the token file is not a private
regular file of 64 hex digits; delete it and start again), `daemon_port_foreign`,
`daemon_port_unavailable` (no free port near this HOME's), `daemon_stop_failed`
(an old daemon of this HOME did not stop), `engine_auth_disabled`,
`engine_auth_rejected`, and for Postgres `postgres_setup_failed`,
`postgres_binaries_missing`, `postgres_start_failed`, `postgres_stop_failed`,
`postgres_control_failed`, `postgres_state_unknown`, `postgres_instance_invalid`,
`postgres_instance_unsafe`, `postgres_socket_dir_unsafe`,
`postgres_socket_path_too_long`, `postgres_socket_path_unsupported` (a quote,
comma or backslash in the HOME path) and `postgres_instance_missing` (a data
directory without pg0's `instance.json`, which pg0 deletes when one of its own
starts fails; restore the file from a backup).

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
--retain-smoke` or a synthetic retain does.

## Running as a user service

```bash
lifecontext service install --maintenance --dry-run   # print the files and commands
lifecontext service install --maintenance             # write, enable and start
lifecontext service install --no-start                # write and enable for the next login only
lifecontext service status                            # units and the last maintenance run
lifecontext service uninstall --maintenance           # remove only the maintenance timer
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

A failed retain keeps exact custody and a durable failed operation. Inspect
`operations get <operation-id> --json`; repeating the same idempotency key
replays its recorded result without a duplicate submission. For a known
provider failure, use `curate reindex <source-id> --expected-version <N>` with
an explicit new idempotency key after repairing the route. For projection
failure, use reconciliation after resolving permissions or reviewing the edit;
never edit the catalog or overwrite conflicting Markdown. Restart recovery
uses exact provider evidence and managed hashes; unknown outcomes remain
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
has one; `Related` pages link to their projected files and show a plain title
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
`sudo apt-get install tesseract-ocr`). The Python dependencies already include
image and PDF rendering support. Check `tesseract --version` and
`tesseract --list-langs`; the current extraction command uses Tesseract's
installed English/Russian/Ukrainian language data (`tesseract-ocr-rus` and
`tesseract-ocr-ukr` provide the latter two). Restart `lifecontext serve` after
installation because executable availability is discovered at startup.
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

Managed wiki responses may be a single JSON object/envelope or one standalone
JSON Markdown fence. Prose wrappers, duplicate keys, invalid schemas, and
unsupported citations remain rejected. Each document compiles into its own page
(`source_<source id>`, titled like the document); recompiling a document replaces
that page's claims instead of adding to them. A page holds at most 48 claims from
one document (8 on shared topic and entity pages) under the 48 KB artifact
ceiling: a longer document's evidence windows are sampled evenly from its first
to its last page, and a page that fills up early stops asking the model rather
than failing. Each evidence window requests one short claim for the document's
own page; windows never create other pages. After the summary below, one
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
keep its page from being rewritten). A forgotten or deleted document
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
pages as fit in 32 links and 48 KB. An entry shows the page's first summary
sentence (labelled) or, without a summary, its first current claim that is not
copyright, ISBN or publisher imprint boilerplate. An index problem never fails a document,
and an index whose managed text was edited by hand is left alone until resolved. The HTTP service compiles wiki patches and category suggestions directly
with the pinned LLM provider using the same validated model, URL, reasoning
effort, and private credential file. Compilation supplies bounded evidence
and no tools; Hindsight recall/reflect remains available for retrieval. Each
compile call has a 180-second deadline, no automatic transport retry, and
closes its provider client. A response rejected by schema/citation validation
may be regenerated once for that window after rechecking the source; a second
rejection stops publication. OpenAI chat-compatible compilation requests
JSON-object mode, which the selected endpoint must support. Schema and literal-citation validation still gate publication.
