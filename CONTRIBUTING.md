# Contributing

Thanks for helping with the CoSens SDK. The repository holds two packages that follow one contract: `python/` (`cosens-sdk` on PyPI) and `typescript/` (`cosens-sdk` on npm). `contract/` holds the shared definitions both must match.

## Before you start

For anything larger than a fix, open an issue first so we can agree on the shape. A change to a public method or a request/response field touches both packages and `contract/`; keep them in step in one pull request.

## Develop and test

Python (3.10 or later):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e "python[test]"
PYTHONPATH=python/src pytest python/tests
```

TypeScript (Node 22 or later):

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

Maintainers publish by pushing a tag: `python-vX.Y.Z` for PyPI and `typescript-vX.Y.Z` for npm. The tag must match the version in the package; the workflow checks this.

## Security

Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution is licensed under Apache-2.0, the license of this repository.
