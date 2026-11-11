# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue or pull request for a security problem.

Include the SDK (Python or TypeScript) and version, what you did, what you expected and what happened. Do not include real API keys or user data; redact them.

We aim to acknowledge a report promptly and tell you whether we treat it as a vulnerability. Fixes ship as a patch release on PyPI and npm, with a note in [CHANGELOG.md](CHANGELOG.md).

## Supported versions

Only the latest release of each package receives security fixes.

## Scope

In scope: code in this repository, including what the packages send to the CoSens API and what they write to disk. Out of scope: the CoSens service itself and third-party model gateways; report problems there through your CoSens contact.

## Credentials

The SDK reads only `COSENS_API_KEY` and `COSENS_REGION`. If you have posted a key in an issue, log or commit by mistake, revoke it before anything else.
