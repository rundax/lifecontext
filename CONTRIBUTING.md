# Contributing to LifeContext

Thank you for helping. Bug reports, documentation fixes and pull requests are
welcome. LifeContext is source-available under the
[Business Source License 1.1](LICENSE); see [LICENSING.md](LICENSING.md).

## Before you start

- **Security problems** go to [SECURITY.md](SECURITY.md), never to a public
  issue.
- For anything larger than a small fix, open an issue first and describe the
  change, so we can agree on the approach before you write it.
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Contributor License Agreement

Every commit author signs the [CLA](CLA.md) before a pull request is merged.
It lets the licensor offer commercial licenses for code that includes your
work. You keep your copyright.

To sign, add this comment to your first pull request:

> I have read the LifeContext CLA (CLA.md, version 1.0) and I hereby sign it.

You only sign once.

## Development setup

[docs/development.md](docs/development.md) describes the development
environment, the dev launcher and the test commands. In short, from the
repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -c constraints-cpu.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu -e .
cd tests
PYTHONPATH=../src:. ../.venv/bin/python -m unittest test_journal_names test_projection_names -v
```

Tests write their scratch files under `.dev/tmp` in the checkout. The full
suite and the lint check run in GitHub Actions on every push and pull request
(`.github/workflows/ci.yml`).

## Pull requests

- Keep each pull request to one change, with a clear description of what and
  why.
- Add or update tests for behaviour changes, and run the affected tests.
- Run `ruff check src tests scripts .github/scripts` (rules in `pyproject.toml`)
  on the files you touched and do not add new
  findings.
- Update the documentation (`README.md`, `docs/`) when behaviour or commands
  change, and add one bullet under `# Unreleased` in
  [CHANGELOG.md](CHANGELOG.md).
- Use only synthetic data in tests, fixtures, issues and screenshots. Never
  commit real journal entries, documents, vault files, API keys, tokens or
  `.dev/` contents.
- Do not rename stored identifiers such as `~/.lifeweave`, the `lifeweave`
  profile and bank, vault markers or media types. They are listed under
  [Compatibility identifiers](docs/development.md#compatibility-identifiers).
- In documentation and text, call LifeContext "source-available", never
  "open source".

## Reporting bugs

Use the bug report form. Include `lifecontext --version`, your operating
system, the exact command and its JSON output with personal data removed.
