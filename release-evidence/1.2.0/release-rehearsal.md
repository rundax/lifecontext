# LifeContext 1.2.0 release rehearsal (v1.2.0rc1)

`release.yml` ran for real once, on a throwaway tag, before the repository goes
public. Everything it does at a release happened; the release stayed a draft,
as the workflow keeps every pre-release.

| | |
|---|---|
| Release run | [Release #39314725607](https://github.com/Hantok/lifecontext/actions/runs/39314725607), tag `v1.2.0rc1` on `4466009` (the lane at `8dce6f9` with every version pin set to `1.2.0rc1`) |
| Check run | [Qualify install paths #39333258529](https://github.com/Hantok/lifecontext/actions/runs/39333258529), `release_tag=v1.2.0rc1`, job `release` on `8dce6f9` |
| Evidence | [`qualify/release-rehearsal.json`](qualify/release-rehearsal.json) (artifact `qualify-evidence-release`) |

## What the release workflow did

- **build** (no write or OIDC permission): checked that the tag, `pyproject.toml`,
  `install.sh` and `compose.yaml` name `1.2.0rc1`; built and checked the wheel
  and sdist; assembled seven assets (wheel, sdist, `install.sh`,
  `constraints-cpu.txt`, the three `requirements-PLATFORM.txt` locks) and
  `SHA256SUMS`.
- **sign**: cosign v3.0.6 signed `SHA256SUMS` keylessly and verified it with
  the identity `release.yml@refs/tags/v1.2.0rc1`, as install.sh does.
- **image**: built linux/amd64 and linux/arm64, pushed
  `ghcr.io/hantok/lifecontext:1.2.0rc1` (no `latest` tag for a pre-release)
  with an SBOM and provenance, and signed it.
- **draft**: created the release as a draft pre-release with the seven assets,
  `SHA256SUMS` and `SHA256SUMS.sigstore.json`.
- **publish**: skipped, as for every pre-release.

The first attempt ([run 39313033203](https://github.com/Hantok/lifecontext/actions/runs/39313033203))
failed in **sign**: the transparency log answered 409 to cosign's retried
upload of an entry it had already stored. Each signing step now retries with
a fresh signature (`8dce6f9`), and the tag was cut again.

## What the check run proved

| Check | Result |
|---|---|
| `release_assets` | `gh release download` with the workflow token fetched the draft's nine files; the release is a draft and a pre-release |
| `install_sh_release` | the release's own `install.sh --assets DIR` exited 0 in 99 s: the wheel and `requirements-linux-x86_64.txt` matched `SHA256SUMS`, the dependencies installed with hashes required |
| `release_signature_checked` | install.sh verified `SHA256SUMS.sigstore.json` with cosign v3.0.6 against `release.yml@refs/tags/v1.2.0rc1` |
| `doctor_release` | the installed 1.2.0rc1 ran as a systemd user service; doctor: no problems, no warnings, models cached and the first start offline |
| `image_pull` | `docker pull ghcr.io/hantok/lifecontext:1.2.0rc1` with the workflow token; digest `sha256:811b4c8293202a644f9e990e73f052bd2d5e322bff5e903a53122d6a5c2ea3c5` |
| `image_signature` | `cosign verify` of the image with the same identity passed |

## Notes for the real release

- The GHCR package `lifecontext` is **private** after the push, as the
  repository is. Make it public in its package settings right after the
  repository goes public, or `docker pull` and the Compose file fail for
  everyone else.
- Keyless signing writes to the public Sigstore transparency log. The two
  rehearsal signatures and the image signature are there now, naming
  `Hantok/lifecontext`, its `release.yml` and the tag `v1.2.0rc1`.
- Turn on immutable releases in the repository settings before tagging
  `v1.2.0` (see "Releasing" in docs/development.md).
- Anonymous downloads of draft assets do not work while the repository is
  private; the check used the workflow token. The public one-line command was
  not exercised.

## Clean-up

The draft release, the tag `v1.2.0rc1` and the branch
`rehearsal/v1.2.0rc1` are deleted. The image
`ghcr.io/hantok/lifecontext:1.2.0rc1` is still in the private package:
deleting a package version needs the `delete:packages` scope, which the
maintainer's CLI token does not have. Delete it in the package settings (or
with such a token) before making the package public.
