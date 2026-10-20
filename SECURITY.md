# Security policy

## Reporting a vulnerability

Please report security problems privately. Do not open a public issue, pull
request or discussion for them.

1. **GitHub private vulnerability reporting (preferred).** On the repository
   page open **Security → Report a vulnerability**. The report is visible only
   to you and the maintainer.
2. **Email.** Write to **roman@rundax.com** with "LifeContext security" in the
   subject.

Include the LifeContext version (`lifecontext --version`), your operating
system, the steps to reproduce, and what an attacker gains. Use synthetic data:
never send real journal entries, documents, vault files, API keys or tokens.

## What to expect

- We acknowledge your report within **two business days**.
- We confirm or rule out the problem, agree a disclosure date with you, and
  credit you in the release notes unless you prefer otherwise.
- Fixes are released as a new patch version and noted in
  [CHANGELOG.md](CHANGELOG.md).

## Supported versions

| Version | Security fixes |
|---|---|
| 1.2.x (latest release) | Yes |
| Earlier than 1.2.0 | No |

Only the latest minor release receives fixes. Upgrade before reporting if you
can.

## Scope

In scope: the `lifecontext` package and command, its loopback HTTP API, the
handling of `~/.lifeweave` (configuration, credentials, the API token, the
original-document archive and the catalog), writes to the configured vault, the
scripts in `scripts/`, and the agent skill in `skills/`.

Out of scope: vulnerabilities in Hindsight, PostgreSQL, Python packages,
Tesseract, models or LLM providers themselves. Report those upstream; tell us
too if LifeContext's use of them makes the problem worse.

## Local isolation

LifeContext separates your data from other accounts on the same computer: its
API and the Hindsight memory engine listen on loopback only and require tokens
from files only your account can read, and the engine's PostgreSQL listens on a
private Unix socket, not on TCP. It does not, and cannot, protect your data from
programs running under your own account or from the administrator (root).
[docs/privacy.md](docs/privacy.md#other-users-of-this-computer) lists what is
protected and the few seconds on first start when it is not. A way for another
local account to read or change your data is in scope.
