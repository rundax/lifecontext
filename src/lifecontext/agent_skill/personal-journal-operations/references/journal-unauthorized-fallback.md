# Authorization failure

Stop on authorization failure. Use the LifeContext CLI described in
[personal-journal-operations](../SKILL.md); do not bypass authorization through
provider internals or change an owner allowlist. Report the error and the
intended instance without exposing credentials.

`token_unavailable` means this account has no API token: the service creates
it when it starts under the same user and HOME. Report it; never create, copy
or read a token yourself. `unauthorized` means the token was refused or is
unsafe to read.
