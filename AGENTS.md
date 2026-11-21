# CoSens SDK Agent Rules

## Version numbers

Python (`python/pyproject.toml`) and TypeScript (`typescript/package.json`) are versioned separately, but both follow the same rule:

- Bump the **patch** number (third digit, `0.9.2` → `0.9.3`) for ordinary releases: small features, fixes, renamed fields, doc changes.
- Bump the **minor** number (second digit, `0.9.x` → `0.10.0`) only for a major feature.
- A breaking change on its own is not a reason to bump the minor number. Record it under "Breaking" in `CHANGELOG.md`.

Release by tagging `python-v<version>` or `typescript-v<version>`; `.github/workflows/release.yml` publishes it.
