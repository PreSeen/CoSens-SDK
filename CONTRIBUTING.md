# Contributing

Thanks for helping with the CoSens SDK. The repository holds the RSI SDK in two languages with the same atomic API: `python/` (`cosens-sdk` on PyPI) and `typescript/` (`cosens-sdk` on npm).

## Before you start

For anything larger than a fix, open an issue first so we can agree on the shape. A change to a public method or field touches both packages; keep them in step in one pull request.

## Develop and test

Python (3.10 or later):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e "python[test]"
PYTHONPATH=python/src pytest python/tests
```

TypeScript (Node 22.12 or later):

```bash
npm --prefix typescript ci
npm --prefix typescript run check
```

`check` builds, type-checks and runs the tests. Tests must not call a real model or the live CoSens API.

## Pull requests

- Branch from `main` and open the pull request against `main`.
- Keep one topic per pull request, with tests for the behaviour you change.
- Add a line to [CHANGELOG.md](CHANGELOG.md) under the package it affects when users can see the change.
- Do not change a package version; maintainers do that when they cut a release.
- Never commit API keys, `.env` files or real user data.

## Releases

Maintainers publish by pushing a tag: `python-vX.Y.Z` for PyPI and `typescript-vX.Y.Z` for npm. The tag must match the version in the package; the workflow checks this. The `Release` workflow (`.github/workflows/release.yml`) tests, builds and publishes; nobody publishes from a laptop.

### Python (PyPI)

PyPI trusts this repository through Trusted Publishing, with the `pypi` environment; no token is stored.

1. In a pull request, set `version` in `python/pyproject.toml` and add a `## Python X.Y.Z` section to the changelog. Merge it.
2. Tag the merge commit and push the tag: `git tag python-vX.Y.Z <commit> && git push origin python-vX.Y.Z`.
3. Check the `Release` run and https://pypi.org/project/cosens-sdk/.

### TypeScript (npm)

The npm package is `cosens-sdk`. The job publishes with `npm publish --provenance --access public` using the secret `NPM_TOKEN`; a version with a prerelease suffix (`0.11.0-rc.1`) goes to the `next` dist-tag instead of `latest`.

One-time setup, by someone who can publish `cosens-sdk` on npm and administer this repository:

1. On npmjs.com, sign in with the account (or organization member) that will own `cosens-sdk`, with two-factor authentication on. Create a granular access token: read and write permission on packages, "Bypass two-factor authentication" enabled, and for the first release "All packages" (no package exists to select yet); after the first release, narrow it to `cosens-sdk`. Granular write tokens expire, so note the date and rotate it.
2. In GitHub, add it as secret `NPM_TOKEN` of the `npm` environment (Settings → Environments → npm → Add environment secret), or as an organization secret available to this repository.
3. Optional, after the first release: in the package settings on npmjs.com, add this repository and `release.yml` as a Trusted Publisher, so later releases need no token.

Each release:

1. In a pull request, set `version` in `typescript/package.json` (run `npm --prefix typescript install --package-lock-only` to update the lockfile) and add a `## TypeScript X.Y.Z` section to the changelog. `npm --prefix typescript run check` must pass; it also checks what `npm pack` would ship. Merge it.
2. Tag the merge commit and push the tag: `git tag typescript-vX.Y.Z <commit> && git push origin typescript-vX.Y.Z`.
3. Check the `Release` run, then `npm view cosens-sdk version` and the provenance badge on https://www.npmjs.com/package/cosens-sdk.

npm never accepts a version number twice, even after it is unpublished. Versions 0.1.0 to 0.5.3 were used by an earlier SDK and are unavailable.

## Security

Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution is licensed under Apache-2.0, the license of this repository.
