# GitHub Actions dependency pins

All external Actions use full commit SHAs. A version comment beside each pin
keeps the reviewed release visible.

| Action | Reviewed release | Full commit SHA | Official update source |
|---|---:|---|---|
| `actions/checkout` | `v7.0.0` | `9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0` | https://github.com/actions/checkout/releases |
| `actions/setup-python` | `v7.0.0` | `5fda3b95a4ea91299a34e894583c3862153e4b97` | https://github.com/actions/setup-python/releases |
| `actions/cache` | `v6.0.0` | `2c8a9bd7457de244a408f35966fab2fb45fda9c8` | https://github.com/actions/cache/releases |
| `actions/configure-pages` | `v6.0.0` | `45bfe0192ca1faeb007ade9deae92b16b8254a0d` | https://github.com/actions/configure-pages/releases |
| `actions/upload-pages-artifact` | `v5.0.0` | `fc324d3547104276b827a68afc52ff2a11cc49c9` | https://github.com/actions/upload-pages-artifact/releases |
| `actions/deploy-pages` | `v5.0.0` | `cd2ce8fcbc39b97be8ca5fce6e763baed58fa128` | https://github.com/actions/deploy-pages/releases |

Pins were resolved from the official Git tags on 2026-10-14. GitHub recommends
full-length commit SHAs when a workflow does not use an Actions dependency
lockfile. See:

- https://docs.github.com/actions/how-tos/write-workflows/choose-what-workflows-do/find-and-customize-actions#using-shas
- https://docs.github.com/actions/reference/security/secure-use

Dependabot checks the `github-actions` ecosystem weekly. Review each proposed
commit against the action's official release page, changelog, runner-version
requirements, and signed tag before merging. A moving major tag is an update
signal, not a trusted workflow reference.

Dependabot also checks the root `pip` requirements against PyPI each week:
https://pypi.org/. Review release notes and the Python 3.11 through 3.15 CI
results before merging a dependency update.

The `frontend-tests` npm lockfile is checked weekly as a separate Dependabot
ecosystem. Browser smoke tests install with `npm ci`, so CI uses the reviewed
versions recorded in `frontend-tests/package-lock.json`.

## Map asset toolchain

The data workflow builds Tippecanoe `2.79.0` from full source commit
`68ab8dcc229f95b8b25877697d5e8d66783af503`. Review future releases and tags
from the official repository before changing both values:
https://github.com/felt/tippecanoe/releases/tag/2.79.0.

The workflow verifies the checked-out source commit and the compiled binary's
reported version. The snapshot manifest records both values and the SHA-256 of
`docs/assets/build-map-assets.mjs`.
