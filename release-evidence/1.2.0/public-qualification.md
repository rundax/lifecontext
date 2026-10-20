# LifeContext 1.2.0: public qualification

The first public commits were qualified in this repository, on clean
GitHub-hosted runners, with the same scenario as
[qualification.md](qualification.md) against a real model.

| | |
|---|---|
| Commit | `a3a6eabdc4d12fa4c980951b7b81dec8756bba75` (`main`) |
| Run | [Qualify install paths #39404142191](https://github.com/rundax/lifecontext/actions/runs/39404142191), `workflow_dispatch` with `macos=true`, 2026-10-20 |
| CI | [run 39404050926](https://github.com/rundax/lifecontext/actions/runs/39404050926) on `main`: tests, wheel, dependency locks and the install-isolation test green (`ruff` is not blocking yet and reports older findings only) |
| Evidence | the run's artifacts, copied unchanged to [`qualify-public/`](qualify-public/) |

## Results

| Job | Runner | Checks | Result |
|---|---|---|---|
| native (`install.sh`) | `ubuntu-24.04`, x86_64 | 37 | all passed |
| docker (Compose) | `ubuntu-24.04`, x86_64 | 37 | all passed |
| macos (`install.sh`) | `macos-14`, arm64 | 10 | all passed |

Timings from this run: `install.sh` 63 s on Ubuntu and 78 s on macOS; image
build 140 s; container healthy 30 s after start; container memory 1.72 GiB
after the scenario against a 4 GiB limit.

Each check, what it proves and what is not covered yet are described in
[qualification.md](qualification.md). Not covered here: the public
`curl … | sh` command and the published image, which exist only after
`v1.2.0` is tagged; that check runs as `qualify.yml -f release_tag=v1.2.0`
against the draft release.
