# Security policy

## Supported versions

Only the latest release and `main` receive fixes while the project is in alpha.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/boowenchen/zh-community-radar/security/advisories/new)
instead. Include steps to reproduce, the affected version or commit, and the impact you expect.

You should get a first reply within 7 days. Once a fix is ready we will publish an advisory and
credit you unless you prefer otherwise.

## Scope notes

Things we especially care about:

- Ways to make Radar fetch hosts outside a source's allowlist (redirects, URL parsing, SSRF).
- Leaking API keys or environment variables into prompts, logs, receipts or digests.
- Prompt injection from fetched content that changes model parameters, endpoints or tools.
- Path traversal in backup, restore or raw-cache handling.

Radar never needs credentials for the sites it reads. If you find a code path that sends cookies
or tokens to a source, that is a bug.
