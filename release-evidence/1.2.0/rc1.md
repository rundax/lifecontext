# v1.2.0rc1, the first release candidate

Published 2026-10-22 as a pre-release of `rundax/lifecontext`, following
`docs/development.md`, "Releasing", steps 1 to 11. Immutable releases were
still off, so this pre-release can still be edited; they must be on before
the final.

| Step | Run | Result |
|---|---|---|
| Release branch `release/v1.2.0rc1` at `9bf1748` (`main` at `eb29a6f` plus the version in the five files) | CI [39837237177](https://github.com/rundax/lifecontext/actions/runs/39837237177) | tests, wheel and locks pass; `ruff` fails with the known findings that `main` also has (not blocking) |
| Full qualification on the branch (`-f macos=true`) | [39837253349](https://github.com/rundax/lifecontext/actions/runs/39837253349) | every recorded check passes: native 39, Docker 38, macOS 11, wrong key 3 |
| Tag `v1.2.0rc1`, `release.yml` | [39838648496](https://github.com/rundax/lifecontext/actions/runs/39838648496) | build, sign, image and draft pass; a draft pre-release with 9 assets |
| Check of the draft (`-f release_tag=v1.2.0rc1`) | [39841010055](https://github.com/rundax/lifecontext/actions/runs/39841010055) | 7 of 7 pass: install with a verified signature, doctor, model settings, image pull and signature; `package_visibility` private |
| Published with `gh release edit v1.2.0rc1 --draft=false --prerelease` | | `releases/download/v1.2.0rc1/install.sh` answers 200 without a login; `releases/latest/download/install.sh` stays 404 |
| Public install (`-f public_tag=v1.2.0rc1`) | [39842379094](https://github.com/rundax/lifecontext/actions/runs/39842379094) | Linux and macOS: 11 checks each pass with no token, among them the anonymous cosign check, `lifecontext 1.2.0rc1`, the user service, doctor and the model probe (retain and reflect); the image is not anonymously pullable while the package is private |

Every qualification used the CI gateway (`openai_compatible`). The Anthropic
end-to-end run (Spike C) waits for the `LIFECONTEXT_CI_ANTHROPIC_API_KEY`
secret.
