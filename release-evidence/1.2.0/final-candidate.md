# LifeContext 1.2.0: final candidate qualification

This run qualifies the merged release candidate: the install paths, the
reviewed packaging and wiki fixes, and the engine authentication (a token on
the Hindsight API, socket-only Postgres, per-home engine ports). The detailed
description of each check, sizes and the release rehearsal are in
[qualification.md](qualification.md), measured on the same scenario one run
earlier, before the engine authentication was merged.

| | |
|---|---|
| Commit | `21c36dfd880ee66159f32e6ad8fe6061c60e5893` (branch `release-candidate`: `integration-2` at `3aa1e94` + `fix/hindsight-auth` at `cd2a6a6` + `lane-c2/release` at `ba0e804`) |
| Run | [Qualify install paths #39337306385](https://github.com/Hantok/lifecontext/actions/runs/39337306385), `workflow_dispatch` with `macos=true`, 2026-10-19 |
| CI | [run 39337306595](https://github.com/Hantok/lifecontext/actions/runs/39337306595) on the same commit: tests, wheel and the dependency-lock check green (`ruff` is not blocking yet and reports older findings only) |
| Evidence | the run's artifacts, copied unchanged to [`qualify-final/`](qualify-final/) |

## Results

| Job | Runner | Checks | Result |
|---|---|---|---|
| native (`install.sh`) | `ubuntu-24.04`, x86_64, 2 vCPUs | 37 | all passed |
| docker (Compose) | `ubuntu-24.04`, x86_64, 2 vCPUs | 37 | all passed |
| macos (`install.sh`) | `macos-14`, arm64 | 10 | all passed |

What this run adds to the earlier one:

- The memory engine started with its token and Postgres on a private socket
  on all three platforms; `doctor` reported no problems and no warnings
  everywhere.
- Inside the container the only listeners are the LifeContext API
  (`127.0.0.1:8765`), the engine on its per-home port (`127.0.0.1:25994`)
  and Docker's resolver; Postgres has no TCP listener.
- Backup with `--stop-service`, restore into a fresh home or fresh volumes,
  re-run and upgrade of `install.sh`, and uninstall all passed with the
  authenticated engine.
- Timings: `install.sh` 78 s on Ubuntu and 62 s on macOS; image build 197 s;
  container healthy 35 s after start; container memory 1.73 GiB after the
  scenario against a 4 GiB limit.

Not covered yet, as in qualification.md: the public `curl … | sh` command
and the public image, which exist only after the repository is public and
`v1.2.0` is tagged; that check runs as `qualify.yml -f release_tag=v1.2.0`
against the draft release.

## Commit IDs and run links

The commit IDs, branch names and workflow runs in this record belong to the
private repository in which LifeContext was developed before publication;
they are not publicly visible. That history was squashed into the first
public commit, whose tree is the qualified tree plus this note. The first
public commit is qualified again in this repository, in its own record.
