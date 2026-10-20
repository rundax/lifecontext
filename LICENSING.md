# Licensing FAQ

LifeContext is **source-available, not open source**. It is published under the
[Business Source License 1.1](LICENSE) (BUSL-1.1) with an Additional Use Grant.
Each version becomes available under the Apache License, Version 2.0 on its
Change Date. BUSL-1.1 is not an OSI-approved license.

This page explains the license in plain words. The [LICENSE](LICENSE) file is
what applies. This is not legal advice.

## The Additional Use Grant

The grant in [LICENSE](LICENSE), verbatim:

> You may make production use of the Licensed Work in the following cases:
>
> (a) You are a natural person, and you run your own copy of the Licensed Work
> for your use alone, directly or through software agents acting for you. This
> includes your work as an employee, contractor, freelancer or sole proprietor,
> and documents that belong to your employer or your clients.
>
> (b) You are a non-profit organization, an accredited educational institution,
> or a non-profit or publicly funded research institution, and you use the
> Licensed Work for your own purposes.
>
> This grant does not extend to:
>
> (i) an organization not described in (b) installing, deploying, providing or
> administering the Licensed Work for use by its employees, contractors,
> members or other people; or
>
> (ii) offering the Licensed Work, or a derivative work of it, to third parties
> as a hosted, managed, embedded or other service that gives them access to its
> functionality.
>
> Work that you produce with the Licensed Work and deliver to others, such as
> reports, advice or summaries, is not a service under (ii). Nothing in this
> grant limits the non-production use permitted by this License.

Beyond the grant, BUSL-1.1 lets everyone copy, modify, create derivative works,
redistribute, and make non-production use of LifeContext.

## The short version

| Who | Production use | Cost |
|---|---|---|
| One person running their own copy for their own use, including at work and with documents that belong to their employer or clients | Allowed | Free |
| A non-profit organization, an accredited educational institution, or a non-profit or publicly funded research institution, for its own purposes | Allowed | Free |
| Anyone, for non-production use (reading, evaluating, testing, development) | Allowed | Free |
| An organization installing, deploying, providing or administering LifeContext for its staff or other people | Needs a commercial license | Paid |
| Anyone offering LifeContext or a derivative as a hosted, managed, embedded or other service | Needs a commercial license | Paid |
| Anyone, for a version after its Change Date | Allowed under Apache-2.0 | Free |

## Common questions

**I use it at work, with my employer's documents.** Free, when you run your own
copy for yourself. The license only covers LifeContext; it does not authorize
you to process those documents. Your employer's rules and your confidentiality
duties still apply, including to the text that LifeContext sends to the LLM
provider you configure (see [docs/privacy.md](docs/privacy.md)). If your
employer installs, provides or manages LifeContext for you, the employer needs
a commercial license.

**I am a freelancer or sole proprietor and use it for client work.** Free. The
reports, advice or summaries you deliver are your work, not a service under
(ii). Giving clients access to LifeContext itself needs a commercial license.

**Several people at one company use it.** Each person may run their own copy
for their own use. A copy that the company provides, or an instance that
several people share, needs a commercial license.

**I am a one-person company.** Free while you are the only user.

**AI agents act for me.** Agents that run your copy on your behalf, such as
Claude Code or Codex using the LifeContext skill, are your own use.

**A company wants to evaluate it.** Free. Reading the source, trying it on
synthetic data and testing an integration are non-production use, which
BUSL-1.1 grants to everyone.

**Can I fork it?** Yes. You may change it and share your changes. Keep the
[LICENSE](LICENSE) with every copy; the same terms apply to your fork.

**Is LifeContext open source?** No. You can read, change and redistribute the
source, but some production use needs a commercial license. Call it
"source-available".

**Can I contribute?** Yes. Contributors sign the [CLA](CLA.md) before a pull
request is merged. It lets the licensor offer commercial licenses for code that
includes your contribution. See [CONTRIBUTING.md](CONTRIBUTING.md).

**What about the libraries and models it uses?** They keep their own licenses.
See [NOTICE](NOTICE).

**Can I use the name "LifeContext"?** The license grants no rights to the name
or any logo. Ask before you name a product or service after it.

**My case is not listed.** Ask at roman@rundax.com before you rely on it.

For a commercial license, see [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

## What the Change Date means

Every released version has its own Change Date. BUSL-1.1 converts a version to
the Change License on its Change Date or on the fourth anniversary of that
version's first public distribution, whichever comes first. From then on anyone
may use that version under the Apache License, Version 2.0, including
businesses.

- LifeContext 1.2.0: Change Date 2030-11-08, so it becomes Apache-2.0 **on or
  before 2030-11-08**.
- Later versions get their own Change Date, about four years after their
  release. The date is in the `LICENSE` file of that version.

The Change Date does not affect newer versions: code released later stays under
BUSL-1.1 until its own Change Date.
