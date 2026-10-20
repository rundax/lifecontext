# LifeContext 1.2.0 qualification: install.sh and Docker

Both public install paths, `install.sh` and the Docker image with Compose,
were installed on clean GitHub-hosted runners and put through one synthetic
scenario each against a real model. Every check passed.

| | |
|---|---|
| Commit | `90d6c387bf6303d80a6917a69654fc596046ddb4` (branch `lane-c2/release`: the install paths on the candidate base `integration-2` at `3aa1e94`, with the review fixes) |
| Run | [Qualify install paths #39333805153](https://github.com/Hantok/lifecontext/actions/runs/39333805153), `workflow_dispatch` with `macos=true`, attempt 1, 2026-10-19 22:16–22:28 UTC |
| CI | [run 39333805012](https://github.com/Hantok/lifecontext/actions/runs/39333805012) on the same commit: tests, wheel and the dependency-lock check green (`ruff` is not blocking yet and reports older findings only) |
| Workflow | `.github/workflows/qualify.yml`, scenario `.github/scripts/qualify.py` |
| Package | `lifecontext 1.2.0.dev0`, the wheel built from that commit in the run, dependencies from `requirements/*.txt` |
| Model | an OpenAI-compatible endpoint from repository secrets, model `gemini-3.8-flash-high` (repository variable) |
| Evidence | the run's artifacts `qualify-evidence-native`, `-docker`, `-macos` (kept 90 days), copied to [`qualify/`](qualify/) |

The final release candidate, with the engine authentication merged, passed
the same scenario again: [final-candidate.md](final-candidate.md).

The release workflow and a signed draft release were rehearsed separately:
[release-rehearsal.md](release-rehearsal.md).

## Results

| Job | Runner | Checks | Result |
|---|---|---|---|
| native | `ubuntu-24.04` (image 20261012.334.1), x86_64, 2 vCPUs | 37 | all passed |
| docker | `ubuntu-24.04` (image 20261018.339.1), x86_64, 2 vCPUs, Docker 28.0.4, Compose 2.38.2 | 37 | all passed |
| macos | `macos-14` (image 20260831.0302.1), arm64, 5 vCPUs | 10 | all passed |

Each job lists the checks it must record before it starts; one that never ran
counts as failed. Each job scanned its evidence and summary for the endpoint
(URL, host and path) and the key, ignoring case, before uploading them or
showing the summary, and the final job scanned all of it again; nothing
matched.

## What each check proved

### install.sh on Ubuntu 24.04 (native)

- **Install** (`install_sh`): `sh install.sh --wheel … --lock
  requirements/linux-x86_64.txt --non-interactive`, with `LIFECONTEXT_PROVIDER`,
  `_MODEL`, `_BASE_URL`, `_TIMEZONE` and `LIFECONTEXT_API_KEY_FILE` set, exited
  0 in 95 s: a pinned uv 0.12.24 (checksum verified), uv-managed Python
  3.12.15, the locked dependencies with every hash checked
  (`torch 2.14.0+cpu`), the models at fixed revisions, Tesseract 5.3.4 with
  eng/rus/ukr through `sudo -n apt-get`, `init` from the variables with the
  key read from the file, `service install --maintenance`, `skills install` (a
  stand-in `~/.claude` was linked) and `doctor`.
- **Service** (`systemd_user_session`, `service_running`, `service_ready`):
  after `loginctl enable-linger`, the systemd user manager ran
  `lifecontext.service` and enabled `lifecontext-maintain.timer`; `/healthz`
  answered when install.sh finished.
- **Doctor** (`doctor_installed`): exit 0, no problems and no warnings at all.
- **Capture** (`journal_retain`, `journal_readback`): a synthetic entry
  (`# Title`, `Entry date:`) retained through stdin completed its operation and
  read back byte for byte with the same SHA-256.
- **Documents and OCR** (`document_import_en`, `_uk`, `_image`, `ocr_image`):
  an English and a Ukrainian Markdown document and a PNG of rendered text went
  through custody, extraction, indexing, wiki and projection (34–47 s each);
  the image was read by OCR on its one page, with nothing unresolved.
- **Cited recall** (`recall_cites_document_{en,uk,image}_installed`): for a
  question answered only by the English document, one in Ukrainian answered
  only by the Ukrainian document, and one answered only by the text OCR read
  from the image, the first result came from that document and its passage
  held the expected fact (`K-17`, `42`, `418`).
- **Evidence** (`evidence_readback_*`): the cited source version's SHA-256
  equals the SHA-256 of the imported file.
- **Compiled wiki** (`wiki_literal_quotes_installed`, `wiki_lint_installed`,
  `brief_installed`): the document's wiki page holds the `K-17` claim as a
  literal quote from the document; `wiki lint --fail-on error` exits 0 with no
  errors or warnings; the brief lists all three imported documents.
- **Skills** (`skills_install_check`): `skills install` then `skills check`
  exit 0 with Claude Code linked.
- **MCP** (`mcp_tools`): a minimal stdio JSON-RPC client started
  `lifecontext mcp`, listed 12 read-only tools (no write tools without
  `--allow-writes`) and called `status` successfully.
- **Backup** (`backup`, `service_ready_after_backup`): `backup --stop-service`
  stopped `lifecontext.service` and the Hindsight profile, wrote a 7.5 MB
  archive of 1,496 files and started the service again.
- **Restore** (`restore_fresh_home`, `restored_service_ready`, `*_restored`,
  `restore_keeps_catalog`): the archive restored into an empty HOME; after
  `init` stored the key there (backups never hold it), a second API answered
  the same recall with the same cited document, the journal entry read back
  exactly, and the documents and wiki pages matched the original.
- **Re-run** (`install_sh_rerun`, `rerun_keeps_data`): a second install.sh run
  reused the installed wheel and lock and kept the settings; `config.yaml`,
  `.env` (the same key from the key file), the documents and the wiki pages
  were unchanged and the service came back.
- **Upgrade** (`install_sh_upgrade`, `upgrade_keeps_data`): with the record of
  the installed version removed, install.sh treated the wheel as new: it
  stopped the service, wrote a second backup, synced the locked dependencies,
  installed the wheel, started the service again and ran doctor (31 s); the
  data was unchanged.
- **Uninstall** (`uninstall_keeps_data`): `install.sh --uninstall` removed the
  venv, uv, the command link, the systemd units and the skill link and stopped
  the service, and kept `~/.lifeweave/.env`, `config.yaml`, the catalog, the
  vault and the backups.

### Docker on Ubuntu 24.04

- **Image** (`docker_build`): built from the tree in 148 s; 3.15 GB. The build
  installs the lock with `pip --require-hashes`, fetches both models at fixed
  revisions, loads them with `HF_HUB_OFFLINE=1` as the service user, and
  starts and queries a throwaway embedded Postgres.
- **First start** (`container_healthy`): `docker compose up -d` with
  `LIFECONTEXT_*` variables and the key as a Compose secret (mode 0600, owned
  by uid 1000) set itself up and was healthy in 25 s, with the models loaded
  offline.
- **Isolation** (`no_published_ports`, `non_root_user`, `api_loopback_only`):
  no port bindings and nothing from `docker port`; `docker exec` runs as uid
  1000 (`lifecontext`) and no process in the container runs as root; the API
  listens only on `127.0.0.1:8765`. All listeners in the container are
  loopback (Postgres on 127.0.0.1 and ::1, Hindsight on 127.0.0.1, Docker's
  DNS on 127.0.0.11).
- **Scenario through `bin/lifecontext-docker`** (`doctor_container`, capture,
  documents, OCR, the three cited recalls, evidence, wiki, lint, brief,
  `mcp_tools`): the same checks as native, every command run on the host
  through the wrapper; the documents were host files streamed into the
  container. `wrapper_download_to_host`: `documents download` wrote the
  original on the host with the imported file's SHA-256. Memory: 1.24 GiB
  after start, 1.75 GiB after the scenario, under the 4 GiB limit.
- **Recreate** (`container_healthy_recreated`, `recreate_keeps_data`,
  `*_recreated`): `docker compose up -d --force-recreate` came back healthy in
  25 s with the same documents, wiki pages and cited recall.
- **Clean stop and backup** (`clean_stop`, `backup_in_container`): `docker
  compose stop` took 5 s and left no `postmaster.pid`; `docker compose run
  lifecontext backup` wrote a 7.6 MB archive of 1,498 files whose `.sha256`
  matched after it was copied to the host.
- **Restore** (`restore_in_container`, `container_healthy_restored`,
  `restore_keeps_data`, `*_restored`): the archive restored into a new Compose
  project's empty volumes with `--vault /data/vault`; on start the entrypoint
  stored the key from the secret, and the restored container was healthy in
  25 s and answered the same recall with the same documents and wiki pages.

### install.sh on macOS 14 (arm64)

- `install.sh --lock requirements/macos-arm64.txt` exited 0 in 62 s: uv for
  `aarch64-apple-darwin`, Python 3.12.15, the locked dependencies, the models,
  Tesseract 5.5.3 through Homebrew, `init` from the variables, a launchd agent
  (`io.github.hantok.lifecontext`, loaded and answering) and `doctor` with no
  problems or warnings. No agent was found to link the skill to.
- One journal entry and the English document went through every stage, and
  the recall cited the document first with `K-17` in the passage and the
  evidence SHA-256 matching.

## Caveats

- The native restore check restores into a fresh, empty HOME on the **same
  runner**: it reuses the installed venv and Python, the Hugging Face cache
  (through `HF_HOME`) and the Postgres binaries' download location, and the
  original service is stopped first because both HOMEs would use the same
  Hindsight port. It proves the archive restores and answers; it does not
  prove a restore on a different machine. The Docker restore is closer to that:
  new volumes and a new container, the same image.
- The upgrade check reinstalls the same wheel after removing the record of
  what was installed; it proves the stop, backup, install and start order,
  not a migration between versions.

## Sizes and timings

| Measure | Value |
|---|---|
| Wheel | 307 KB |
| `~/.local/share/lifecontext` total on Linux (venv 2.19 GB, Python 0.12 GB and the uv cache, which shares the venv's files) | 2.44 GB |
| Models fetched by install.sh at fixed revisions (Linux) | 0.58 GB |
| venv on macOS | 1.92 GB |
| Docker image | 3.15 GB |
| install.sh first run / re-run / upgrade, Linux | 95 s / 24 s / 31 s |
| install.sh first run, macOS | 62 s |
| Docker build / first healthy start / recreate / restored start | 148 s / 25 s / 25 s / 25 s |
| Document import through every stage, each | 34–48 s |
| Recall with three documents in the bank | 0.5–1.2 s |
| Backup archive | 7.5–7.6 MB |

A from-source install, where Hindsight downloads the models itself on the
first start, had a 1.01 GB model cache in an earlier run
([39301172686](https://github.com/Hantok/lifecontext/actions/runs/39301172686)).

## Found and fixed during qualification

- The first journal entry after a fresh `init` failed with `vault_unavailable`
  (no `Journal/entries` yet); fixed in the packaging lane.
- `restore` could not fill a vault that is a volume mount point; fixed in the
  packaging lane. Docker restores pass `--vault /data/vault`.
- transformers loaded the reranker from a safetensors conversion pull request,
  which an offline start could not find; the models are now fetched at fixed
  revisions with "main" pointed at them.
- The embedded Postgres (pg0) needs `ps`; `python:3.12-slim` has none, so the
  engine got no database URL and restarted in a loop. The image installs
  `procps` and the libraries pg0 documents, on the Debian 12 base pg0 is built
  on.
- A run lost the PyTorch CPU index for two minutes; uv now has longer timeouts
  and more retries, and a failed install says so.
- The transparency log answered 409 to a retried signing upload in the release
  rehearsal; signing now retries with a fresh signature.

## Not covered yet

- **The public one-line command** (`curl … | sh` against
  `releases/latest/download/install.sh`) needs a public repository and a
  published release. The rehearsal installed from a signed draft's assets with
  the workflow token instead.
- **The published v1.2.0 release and its image**: the GHCR package is private
  until it is made public after the flip, and the rehearsal's rc image is still
  in it.
- **linux/arm64 image at run time** and **native Linux aarch64**: the arm64
  image was built (its build loads the models offline and queries the embedded
  Postgres under emulation) but never started as a container; no aarch64 Linux
  runner ran install.sh.
- **Interactive setup**: `init` through `/dev/tty` under `curl | sh` and the
  container's wait for `docker exec -it … init`; every run was unattended.
- **Other package managers and hosts**: the `dnf` path, a host without
  password-less sudo, a host without a systemd user session, WSL, Docker
  Desktop on macOS or Windows.
- **Upgrade from an older release** with a schema change; there is no earlier
  public release and no `lifecontext upgrade` command.
- **Docker variants**: a bind-mounted vault, and the `lifecontext-docker`
  paths of `curate --input`, `categories propose --input`, `import` and
  `export` (unit tests with a fake `docker` only).
- **Vision**, other providers and models, large or many documents, recall
  under load, and the daily maintenance timer firing.

## Commit IDs and run links

The commit IDs, branch names and workflow runs in this record belong to the
private repository in which LifeContext was developed before publication;
they are not publicly visible. That history was squashed into the first
public commit, whose tree is the qualified tree plus this note. The first
public commit is qualified again in this repository, in its own record.
