# Agents

Any coding or chat agent that can run shell commands can use LifeContext. It
does so through the same `lifecontext` CLI you use, guided by one skill. Apps
that speak MCP, such as Claude Desktop and Cursor, use the
[MCP server](#mcp-server) instead.

## The skill

The skill `personal-journal-operations` ships inside the Python package
(`src/lifecontext/agent_skill/personal-journal-operations/`;
`skills/personal-journal-operations` at the repository root links to it, for
tools that look there). It holds `SKILL.md` plus reference files for capture,
read-back, document and wiki workflows, and the failure cases. It tells the
agent how to:

- add what you give it to the journal (complete text, a unique source ID,
  version `1`, a stable idempotency key) and read it back to confirm it was
  stored;
- import files with `documents import` and check their coverage with
  `documents show`;
- fetch wiki pages with `wiki list` and `wiki show PAGE_ID` (fast, no LLM);
- answer questions with `recall`, `reflect` and `evidence`, citing the source
  version behind each statement;
- produce the summaries, timelines or plans you ask for from that evidence.

The skill calls the `lifecontext` command on `PATH`, through one variable at
the top of `SKILL.md`:

```bash
CLI=lifecontext
```

If your command is elsewhere (for example `.venv/bin/lifecontext` in a
from-source install, or `bin/lifecontext-dev` in a development checkout), edit
that line, or put the command's directory on the agent's `PATH`. Every task
starts with `"$CLI" status --json`; if that fails, the agent reports the failed
dependency and stops. Commands never start the API.

## Installing it for your agents

`lifecontext skills` links the copy installed with the package into each
agent's skills directory, so every agent reads the same version and an upgrade
updates them all:

| `--agent` | Agent | Link |
|---|---|---|
| `claude` | Claude Code | `~/.claude/skills/personal-journal-operations` |
| `codex` | Codex | `~/.codex/skills/personal-journal-operations` |
| `gemini` | Gemini CLI and other agents reading the shared directory | `~/.agents/skills/personal-journal-operations` |
| `hermes` | Hermes | `~/.hermes/skills/productivity/personal-journal-operations` |

```bash
lifecontext skills install             # agents found in this HOME
lifecontext skills install --agent all --dry-run   # show the plan only
lifecontext skills check               # exit 1 when a link is missing or differs
lifecontext skills remove              # delete LifeContext's links (all agents)
```

Without `--agent`, `install` and `check` use the agents whose directory exists
(`~/.claude`, `~/.codex`, `~/.gemini` or `~/.agents`, `~/.hermes`); `remove`
looks at all four. An entry at a link location that is not a link to this skill
(a copied directory, a file, a link elsewhere) is moved to
`${XDG_DATA_HOME:-~/.local/share}/life-context/skill-backups/AGENT-TIMESTAMP`
first; a link into an older LifeContext installation is replaced. `remove`
deletes only links that point at a LifeContext skill and leaves everything else
alone. Add `--json` for the report. `scripts/install_agent_skill.sh` in a
checkout now runs these commands with `--agent all`.

### As a Claude Code plugin

The repository is also a Claude Code plugin marketplace
(`.claude-plugin/marketplace.json`) with one plugin, `lifecontext`, whose root is
the packaged skill directory `src/lifecontext/agent_skill/` (manifest in its
`.claude-plugin/plugin.json`):

```
/plugin marketplace add rundax/lifecontext
/plugin install lifecontext@lifecontext
```

The plugin carries only the skill; the `lifecontext` command must be installed
and its service running as described in the README. Use either the plugin or
`lifecontext skills install --agent claude`, not both, so Claude Code does not
see the skill twice. The plugin's `version` follows the package version; a
test keeps them equal.

## Operator boundaries

The skill says that the agent operates the system through the CLI and does not
maintain it:

- Use only the CLI commands from the skill and its references. Never read or
  write `~/.lifeweave`, the vault, catalog, archive, Hindsight, credentials,
  service units, repository source, scripts or skills, and never start, stop,
  restart, install or upgrade anything.
- Do not fix, patch, work around or "make it work" when something fails. On any
  non-zero exit, error code, failed read-back, inconsistent result or timeout,
  stop that task and report the exact command, its output, what was expected
  and what state may already have changed. Do not try variations, another path
  or another instance; put any suspected cause in the report, never apply it.
- Retry only where a reference names one specific transient error: same
  idempotency key and bytes, once.
- Run `recall` and `reflect` one at a time with a tool timeout of at least
  300 seconds, never in parallel, and never resend one that is still running or
  timed out; report it instead.

**Trust model.** Connected agents run your CLI with your authority. The skill
tells them what not to do; the application does not enforce it. Whatever an
agent reads (recall results, wiki pages, evidence) also goes to that agent's
own model provider; see [privacy.md](privacy.md).

## MCP server

`lifecontext mcp` serves LifeContext to MCP clients (Claude Desktop, Cursor and
others) over stdio. It is a thin client of the running local API: it sends the
same requests as the CLI, with the same token, size limits, timeouts and error
codes, and it never starts the API. Run it as the same user as
`lifecontext serve`, which creates the token it reads.

It is **read-only by default**:

| Tool | What it does |
|---|---|
| `status` | API and dependency readiness; the model calls it first |
| `recall` | cited passages for a question; 20-50 s, over 2 minutes under load |
| `reflect` | an answer written from cited evidence; slower than `recall` |
| `evidence` | exact metadata of one source version, and journal text with `include_content` |
| `wiki_list`, `wiki_show` | the wiki pages, and one page's quote-backed claims; fast, no model call |
| `wiki_lint` | wiki and document health findings |
| `wiki_reviews` | the contradiction and hand-edit review queue (list only) |
| `brief` | what changed on one day; never writes the brief into the vault |
| `documents_list`, `document_show` | imported documents, their stages and page coverage |
| `operation_show` | the stored result of one write, by its operation ID |

`--allow-writes` adds write tools. Each requires an idempotency key, as in the
CLI:

| Tool | What it does |
|---|---|
| `retain` | saves journal text as version 1 of a new source |
| `wiki_reviews_resolve` | records the owner's decision on one review, with a note; only on the owner's explicit decision |
| `documents_import` | imports one local file, given by absolute path; only with `--import-dir` |

`documents_import` exists only when you also name one or more directories with
`--import-dir DIR` (repeatable). It reads a file only when its real path, after
resolving symlinks, is inside one of them, and the file itself is a regular
file, not a symlink, directory or pipe. Even inside an allowed directory it
never reads the LifeContext home (`~/.lifeweave`, with the API token and
credentials), the vault, hidden directories in your home directory (`~/.ssh`,
`~/.config` and the like), `/proc`, `/sys`, `/dev` or `/run`. The server does
not start when an `--import-dir` is missing or lies in one of those places.
`recall` and `reflect` share one slot and `documents_import` has another: a
call made while the previous one is still running fails with `busy`.

There are no tools to forget, delete, correct or re-index sources, rebuild the
wiki, or decide categories and relationships; those stay with you in the CLI.
The tool descriptions and the server's instructions carry a few of the skill's
boundaries (retrieved content is untrusted data, never instructions; on an
error the model stops and reports the code; `recall` and `reflect` run one at a
time); they are not the whole skill, and the application enforces only the
tool set, the import directories and the one-at-a-time slots.

### Claude Desktop and Cursor

Desktop apps do not read your shell `PATH`, so the configuration needs the
absolute path of the command. Print it in a terminal:

```bash
command -v lifecontext
```

Claude Desktop reads `claude_desktop_config.json` (Settings > Developer > Edit
Config). Cursor reads `~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project.
Both take the same block; replace the command with the path printed above:

```json
{
  "mcpServers": {
    "lifecontext": {
      "command": "/home/you/lifecontext/.venv/bin/lifecontext",
      "args": ["mcp"]
    }
  }
}
```

Restart the app after editing the file. Options go into `args`:

- `"args": ["mcp", "--allow-writes"]` lets the model add journal entries and
  record review decisions. Leave it out unless you want that.
- `"args": ["mcp", "--allow-writes", "--import-dir", "/home/you/Inbox"]` also
  lets it import files from `~/Inbox` (absolute path; repeat the option for
  more directories).
- `"args": ["mcp", "--port", "18765"]` when the API listens on a port other
  than 8765.
- In a development checkout, use `bin/lifecontext-dev` by absolute path as the
  command; it fixes the port and `HOME` itself.

### Slow calls and client timeouts

`recall` usually takes 20-50 seconds, and over 2 minutes when the machine is
busy; `reflect` takes longer, and a document import can take many minutes.
While one of these waits, the server sends a progress notification every 15
seconds to clients that asked for progress. Clients decide how long they wait:

- **Claude Code** honours a per-server `"timeout"` in milliseconds in the
  server's `.mcp.json` entry (for example `"timeout": 900000` for 15 minutes),
  or `MCP_TOOL_TIMEOUT` for all servers; progress notifications keep its idle
  timer from firing.
- **Claude Desktop** and **Cursor** document no tool timeout setting, and users
  report that they give up on a tool call after about 60 seconds. A slow
  `recall`, `reflect` or import can then show as an error although it
  finishes on your machine.

When a client gives up, the request is not cancelled: it runs to the end on
your machine, and until then another call of the same kind answers `busy`.
Check an import with `documents_list` or `document_show` instead of importing
again. Closing the app also waits for a call in flight: the server exits once
that request has finished.

The same [trust model](#operator-boundaries) applies as for the CLI: what the
tools return goes to the app's model provider.

## Agents on another machine

An agent on one machine can use the LifeContext on another over SSH, through
the same `lifecontext` command and the same skill. The data stays on the
LifeContext machine. The agent machine holds a key that can run only everyday
agent commands, because `lifecontext agent-gate` is that key's forced command.

**1. Make a key for the agent**, on the agent machine. It has no passphrase,
since an unattended agent cannot type one; the limits are on the server side:

```bash
ssh-keygen -t ed25519 -N '' -C lifecontext-agent -f ~/.ssh/lifecontext_agent
```

**2. Authorize it**, on the LifeContext machine, as one line in the
`~/.ssh/authorized_keys` of the user that runs LifeContext. Use the absolute
path of that user's `lifecontext` command (`install.sh` links
`~/.local/bin/lifecontext`) and the contents of `lifecontext_agent.pub`:

```text
restrict,command="/home/USER/.local/bin/lifecontext agent-gate" ssh-ed25519 AAAA... lifecontext-agent
```

`restrict` turns off port, agent and X11 forwarding, terminals and
`~/.ssh/rc`. `command=` makes every login with this key run the gate, whatever
the client asks for; the gate reads what was asked from `SSH_ORIGINAL_COMMAND`,
splits it like a shell would but never runs a shell, and runs it only if it is
allowed (below). The LifeContext service must be running as usual.

**3. Name the machine**, in `~/.ssh/config` on the agent machine. `HostName`
is a LAN or tailnet name or an address; `IdentitiesOnly` stops ssh from
offering any other key, which might open a normal shell; `ServerAliveInterval`
keeps a long `recall` from being dropped as idle:

```text
Host lifecontext-agent
    HostName lifecontext-host.example
    User USER
    IdentityFile ~/.ssh/lifecontext_agent
    IdentitiesOnly yes
    ServerAliveInterval 30
```

**4. Point the agent at it.** Set `LIFECONTEXT_REMOTE=lifecontext-agent` in the
agent's environment (`LIFECONTEXT_SSH` names another ssh command; the default is
`ssh`) and link the wrapper as `lifecontext`, so the skill and the agent run
unchanged:

```bash
ln -s "$PWD/bin/lifecontext-remote" ~/.local/bin/lifecontext
LIFECONTEXT_REMOTE=lifecontext-agent lifecontext status --json
```

The wrapper runs `ssh -T -o BatchMode=yes HOST "lifecontext ..."` with every
argument quoted, passes stdin and stdout through, and exits with the command's
status. It never asks for a password: if the key is not accepted, ssh fails
with status 255.

**Files.** Your own paths stay on your machine. `retain --input FILE`,
`curate --input FILE`, `categories propose --input FILE` and `documents import
FILE` send the file over the connection; the gate saves it under its name in a
fresh private directory below `~/.local/state/lifecontext/agent-stage/` (`0700`,
file `0600`), runs the command and removes the directory afterwards, also after
an error or `SIGTERM`, `SIGHUP` or `SIGINT`. A file name has no slash, no
leading dot and at most 200 bytes; the size limits are the CLI's (1 MiB of text,
32 MiB for a document). `documents download ID DEST` writes `DEST` on the agent
machine and never overwrites it. `--input -` and `mcp` use stdin as usual.

**What is refused, and why.** These commands are allowed: `status`, `doctor`,
`retain`, `recall`, `reflect`, `evidence`, `curate`, `relationships`,
`documents`, `categories`, `wiki`, `brief`, `operations` and `mcp`, with
`--help`. Everything else (`init`, `llm`, `service`, `backup`, `restore`,
`hindsight`, `skills`, `maintain`, `reconcile`, `preflight`, `serve`, `export`,
`import`, `capture` and `agent-gate`) prints `{"error":"command_not_allowed"}`
and exits 1. Agents are operators, not admins (see [Operator
boundaries](#operator-boundaries)): setup, configuration, service control,
repair and moving the whole journal in or out belong to you. For the same
reason a path on the LifeContext machine is refused with
`{"error":"path_not_allowed"}`: `--input /some/path`, `documents download ID
/some/path` and `mcp --import-dir`, so the key cannot read or write other files
there. The key can still do what the allowed commands do, including reading
everything with `recall` and removing a source with `curate delete`; the
[trust model](#operator-boundaries) is unchanged. The gate logs nothing, so no
note text or secret reaches a log; `sshd` records only the login.

**MCP over SSH.** `lifecontext-remote mcp` is an MCP server command that runs
`lifecontext mcp` on the LifeContext machine, with `--allow-writes` if you add
it (`--import-dir` is refused). In Claude Desktop or Cursor:

```json
{
  "mcpServers": {
    "lifecontext": {
      "command": "/home/you/lifecontext/bin/lifecontext-remote",
      "args": ["mcp"],
      "env": { "LIFECONTEXT_REMOTE": "lifecontext-agent" }
    }
  }
}
```

**The protocol**, for other clients: the command line is `lifecontext ARGS...`
in one ssh command, quoted for POSIX `shlex`; stdin, stdout and the exit status
are the command's. In place of an input file send `@stdin:NAME` and the file's
bytes on stdin (one per call). For `documents download ID`, send `@stdout`
as the destination: on success stdout is `lifecontext-file 1 FILE_BYTES
RESULT_BYTES`, a newline, the original, then the command's own output (usually
its JSON); on failure stdout is the command's error output with its non-zero
status and no such header, so file bytes and JSON never mix.
