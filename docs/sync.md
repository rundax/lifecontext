# Your notes, your devices: Obsidian, capture and sync over Tailscale

This page sets up one personal arrangement end to end. LifeContext runs on one
always-on computer (the server, which can be a small home machine). You write
in Obsidian on your phone and laptop; the notes reach the server, LifeContext
turns them into journal entries and a wiki, and the result comes back to every
device. Agents on the server use the same data through the CLI, the skill or
MCP.

```
 phone / laptop                         server
 Obsidian + LiveSync plugin             CouchDB hub        127.0.0.1:5984 (Docker)
        |                                  ^  |
        | HTTPS, tailnet only              |  | LiveSync headless client
        +--------> tailscale serve --------+  v
                                           vault folder  <--  lifecontext capture
                                           (Journal/Inbox -> Journal/entries, wiki)
                                                ^
                                                |  CLI, skill, MCP (agents)
```

Nothing here is required to use LifeContext. Without sync, open the vault
folder in Obsidian on the computer that runs LifeContext and use only
[step 2](#2-capture-notes-from-the-inbox). Each later step adds one piece and
can be skipped:

| Step | Needs | Optional? |
|---|---|---|
| 1. Vault folder in Obsidian | nothing | the base of everything |
| 2. `lifecontext capture` | the service from [install](install.md) | yes |
| 3. CouchDB hub | Docker; `sudo` once to install Docker and join its group | only for multi-device sync |
| 4. Tailscale | `sudo` once to install and sign in; the tailnet admin turns on HTTPS certificates | only for multi-device sync |
| 5. Headless LiveSync client on the server | Node.js, npm, Git | only for multi-device sync |
| 6. LiveSync plugin on each device | Obsidian | only for multi-device sync |
| 7. Agents on the server | `lifecontext skills install` | yes |

LifeContext is source-available under the Business Source License 1.1.
Self-hosted LiveSync, CouchDB, Docker and Tailscale are separate software
with their own licenses and terms.

## 1. The vault folder, opened in Obsidian

`lifecontext init` creates the vault (default `~/LifeContext/vault`, or the
folder you chose) with `Journal` inside. In Obsidian choose "Open folder as
vault" and pick that folder. LifeContext writes `Journal/entries/YYYY-MM-DD -
Title.md` and the wiki pages under `Journal/wiki/` there; you read them in
Obsidian like any note.

Create the folder `Journal/Inbox` in the vault. It is where you drop notes for
LifeContext to take in (step 2). On a server without a screen, create it with
`mkdir -p` and let the sync (step 5) carry it to your devices.

## 2. Capture notes from the Inbox

```bash
lifecontext capture --once          # one pass over Journal/Inbox
lifecontext capture --once --json   # the same, as stable JSON
lifecontext service install --maintenance --capture   # run it every 30 seconds
```

`capture` turns each Markdown note in `Journal/Inbox` into a journal entry
through the running API, in the same way as `lifecontext retain`. It never
changes or deletes the note. The rules:

- **Settled notes only.** A note is taken in when its bytes have not changed
  for 15 seconds, so a note you are still typing is left alone. With the
  30-second timer an edit reaches the journal about 30 to 60 seconds after you
  stop typing.
- **Once, then again only on a change.** Each note is one source, named from
  its path inside the Inbox (`obsidian_` and a SHA-256 of the path). The first
  capture is version 1; if you edit the note later, the new text is stored as
  the next version of the same entry. Running `capture` again with nothing
  changed does nothing. A renamed or moved note is a new note and becomes a
  new entry.
- **Size and kind.** Only `.md` files of 1 MiB or less, valid UTF-8, not
  empty. A `.md` file that is empty, larger, not UTF-8 or a link to a file is
  skipped and counted under `skipped`; other files, hidden files and folders,
  and links to folders are ignored without a count.
- **Safe to interrupt.** A submission is written to the state folder first and
  sent with a fixed idempotency key, so a run that stops half way is finished
  by the next one without a duplicate. If the service is not running or the API
  fails (a 5xx answer, or the model provider is unreachable), notes wait and are
  counted under `pending`.
- **Refusals are reported, not retried.** If the API refuses a note for good
  (for example `source_conflict` after the entry was forgotten or deleted,
  `conflict` because the version moved on, or `body_too_large`), the run counts
  it under `failed` and lists the note and the error code (`failures` in
  `--json`, a `Refused:` line otherwise). It is reported on every run but not
  sent again until you change the note, which tries it once more.
- **Where it keeps state.** `~/.lifeweave/capture` (mode `0700`, files `0600`):
  what it has seen and sent. `lifecontext backup` includes it. Only one capture
  run goes at a time.

`--inbox DIR` captures another folder; it must be named `Inbox`. Without
`--inbox` the folder is `Journal/Inbox` of the vault in `config.yaml`.

The timer is a `systemd --user` timer (`lifecontext-capture.timer`, every 30
seconds) or a launchd agent (`io.github.hantok.lifecontext.capture`), listed by
`lifecontext service status` and removed by `lifecontext service uninstall`
(`uninstall --capture` removes only it). See
[operations](operations.md#running-as-a-user-service).

## 3. A CouchDB hub that listens only on this machine

Self-hosted LiveSync is an Obsidian plugin that replicates a vault through a
CouchDB database. `docker/livesync/` in the repository holds a hub for it:

- `compose.yaml`: CouchDB pinned by image digest, published on `127.0.0.1:5984`
  only, data in a named volume, the admin account from `couchdb.env`.
- `livesync.ini`: the settings LiveSync needs (CORS for the Obsidian apps, the
  large size limits) and `require_valid_user`, so every request needs an
  account.
- `init-couchdb.sh`: creates the database and a sync user that can use that
  database and nothing else.

```bash
cd docker/livesync
umask 077
printf 'COUCHDB_USER=admin\nCOUCHDB_PASSWORD=%s\n' \
  "$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)" > couchdb.env
docker compose up -d
sh init-couchdb.sh
```

`couchdb.env` is the admin account; keep it private (the folder's
`.gitignore` already excludes it). The script reads it, creates the database
`obsidian-livesync` and the user `livesync`, and writes the sync user's random
password to `sync-user.password` (mode `0600`). It passes passwords to `curl`
through private temporary files, never as arguments, and never prints them. The
sync user is a member of that one database: it cannot list databases, create
them or change their security. Run the script again at any time; it keeps the
database and sets the user's password to the one in the file. `--database`,
`--user`, `--env-file` and `--password-file` change the names and places.

The compose file and the script were checked here against a stand-in for
CouchDB, not against a running container.

## 4. Reach the hub only over Tailscale

Install Tailscale on the server and on every device and sign in to the same
tailnet (installing it and `sudo tailscale up` need `sudo` once). In the
tailnet's admin console, turn on HTTPS certificates. Then publish the hub to
the tailnet:

```bash
sudo tailscale set --operator="$USER"        # once, so serve needs no sudo
tailscale serve --bg --https=443 http://127.0.0.1:5984
tailscale serve status                       # shows the https:// address of the hub
```

`tailscale serve` makes the hub reachable over HTTPS by devices in your
tailnet only, with a certificate Obsidian's mobile apps accept. The hub has no
public address. Never run `tailscale funnel` for it, which would publish it to
the internet, and never publish port 5984 from Docker on another interface.
To limit which tailnet devices may connect, use the tailnet's access policy.

The address from `tailscale serve status` is what you enter as the URI in
steps 5 and 6. CouchDB still asks for the sync user's name and password, so a
device that is on your tailnet but is not set up cannot read the notes.

## 5. The headless LiveSync client on the server

The server has no Obsidian to run the plugin, so the Self-hosted LiveSync
project's command-line client mirrors the hub into the vault folder and back.
These steps follow release 1.0.32 (commit
`7b3b6ff854f2ff10452be3eaf8e0cd76ac65ac2b`) and the README of its CLI at that
commit. You need Git, Node.js and npm.

```bash
git clone https://github.com/vrtmrz/obsidian-livesync.git ~/.local/share/livesync-src
cd ~/.local/share/livesync-src
git checkout 7b3b6ff854f2ff10452be3eaf8e0cd76ac65ac2b
npm ci --ignore-scripts
npm run build --workspace self-hosted-livesync-cli
node src/apps/cli/dist/index.cjs --help
```

`--ignore-scripts` keeps dependencies from running install scripts. Give the
client a protected settings file; never use its `setup <setup URI>` command
here, because the URI would appear in the process list:

```bash
umask 077
mkdir -p ~/.config/livesync ~/.local/state/livesync
HUB_FILES=/path/to/lifecontext/docker/livesync
printf '{\n  "couchDB_URI": "https://HUB-ADDRESS",\n  "couchDB_USER": "livesync",\n  "couchDB_PASSWORD": "%s",\n  "couchDB_DBNAME": "obsidian-livesync",\n  "isConfigured": true,\n  "liveSync": true\n}\n' \
  "$(head -n 1 "$HUB_FILES/sync-user.password")" > ~/.config/livesync/settings.json
```

Replace `HUB-ADDRESS` with the address from step 4. (`printf` is a shell
built-in, so the password is not an argument of any process.) For end-to-end
encryption add `"encrypt": true` and `"passphrase": "..."` by editing the file.

Run the client once in the foreground to see it work (stop it with Ctrl-C),
with `VAULT` set to your vault folder:

```bash
VAULT=~/LifeContext/vault
node src/apps/cli/dist/index.cjs ~/.local/state/livesync daemon \
  --vault "$VAULT" --settings ~/.config/livesync/settings.json
```

It mirrors the vault into the hub, then keeps both sides in step. Then keep it
running as a user service, `~/.config/systemd/user/livesync-client.service`
(replace the Node path with the output of `command -v node`, and the vault
folder if yours is elsewhere):

```ini
[Unit]
Description=Self-hosted LiveSync client for the LifeContext vault

[Service]
ExecStart=/usr/bin/node %h/.local/share/livesync-src/src/apps/cli/dist/index.cjs %h/.local/state/livesync daemon --vault %h/LifeContext/vault --settings %h/.config/livesync/settings.json
UMask=0077
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now livesync-client.service
loginctl enable-linger "$USER"        # keep user services running after you log out
```

The client keeps its own state in `~/.local/state/livesync`, outside the
vault. Conflicting versions of one file are skipped by the client and shown by
its `ls` command; resolve them with its `resolve` command or in Obsidian.
The commands in this section follow the client's README and were not run when
this page was written; start with `--help`.

## 6. The LiveSync plugin on each device

In Obsidian, Settings, Community plugins, install and enable "Self-hosted
LiveSync". In its setup choose CouchDB and enter:

- URI: the `https://` address from step 4.
- Username `livesync`, and the password from `sync-user.password` (move it to
  the device with a password manager, not through chat or email).
- Database name `obsidian-livesync`.

Choose the continuous "LiveSync" mode. Optionally turn on end-to-end
encryption: then CouchDB holds only encrypted data, and every client,
including the one in step 5, must use the same passphrase. Losing it makes the
hub unreadable, but not the vault on the server.

The server's client (step 5) fills the hub from the server's vault, which
LifeContext keeps current. On each device choose the plugin's option to fetch
from the remote hub rather than to overwrite it. The plugin's
"setup URI" carries the password; do not paste one into a command line, where
other users on the machine can read it from the process list.

## 7. Agents on the server

```bash
lifecontext skills install    # Claude Code, Codex, Gemini CLI, Hermes
lifecontext mcp               # Claude Desktop, Cursor: see agents.md
```

An agent on the server reads and adds to the same journal as your notes
(`recall`, `reflect`, `retain`; see [agents](agents.md)). What it reads goes to
its own model provider.

## Check that it works

On a phone or laptop, create `Journal/Inbox/Test note.md` with a few lines
of text. Within about a minute the server's vault has it (`ls
~/LifeContext/vault/Journal/Inbox`), and after the next capture
`~/LifeContext/vault/Journal/entries/` has an entry dated from the note, which
returns to every device. `lifecontext capture --once --json` shows the counts,
`journalctl --user -u lifecontext-capture` the timer's runs, and
`journalctl --user -u livesync-client` the client.

## What moves where

- The hub holds a copy of everything in the vault that the client syncs:
  your notes, LifeContext's entries and wiki pages, and the copies of imported
  originals. Encrypt it end to end if the hub's disk
  is not trusted.
- Notes and entries reach every device you set up. Nothing leaves your tailnet,
  except what LifeContext sends to the model you configured
  ([privacy](privacy.md)).
- Back up the vault and `~/.lifeweave` with `lifecontext backup`
  ([operations](operations.md#backup-and-restore)). The hub is a replica of
  the vault, not a backup of it: a deleted note is deleted on every device. Back
  up the `couchdb-data` volume too if you want the hub's history.
