# zh-community-radar

[繁體中文說明](README.zh-TW.md)

Radar watches public tech communities and turns new posts and selected comments into a short,
evidence-first digest. It covers the usual English sources (Hacker News, GitHub releases, author RSS,
Lobsters, Hugging Face discussions) and also Chinese-language communities that most English tools
skip: **PTT** and **V2EX**.

Every digest line points back to the original post, keeps the time it was observed, and says when
coverage was partial. Radar does not claim to represent "what the whole community thinks"; it
reports what it actually fetched.

> Status: alpha (`0.3.0.dev0`). The collection, event, digest and query pipeline works and is
> covered by tests. There is no hosted service, no built-in sender and no MCP server yet.

## What it does

- **Bounded collection**: per-source intervals, request and time caps, `Retry-After` is persisted
  and respected, allowlisted hosts only, no redirects off the allowlist.
- **Versioned storage**: SQLite keeps every revision of a post/comment, with content hashes and
  checkpoints, so re-runs are idempotent and edits are visible.
- **Events**: related items are merged into events with a stable ID and a quote trail.
- **Optional LLM analysis**: one tool-free model call per event, with run/day/month budgets, a
  shared usage ledger, caching, and machine checks that every quote matches stored evidence.
  Works with the Claude Code CLI or any OpenAI-compatible `/chat/completions` endpoint.
- **Digest and ask**: build an immutable Markdown/JSON digest from checked analyses, or ask a
  question against the local event store (no new web search).
- **Agent-friendly CLI**: every command prints one JSON envelope on stdout with a stable status
  and exit code, so any agent or script can drive it.
- **Backups**: consistent SQLite snapshots with hash and schema verification, and restore into a
  new directory in read-only "shadow" mode.

## Supported sources

| Adapter | Source | Notes |
|---|---|---|
| `hn` | Hacker News (Firebase API) | new + top stories, sampled comment threads |
| `feed` | Any RSS/Atom feed | GitHub `releases.atom` / `tags.atom`, blogs, newsletters |
| `ptt` | PTT web boards (e.g. Soft_Job) | **off by default**, see the ethics note |
| `v2ex` | V2EX public API v1 | hot topics + sampled replies |
| `lobsters` | Lobsters JSON | stories + comments |
| `hf` | Hugging Face model discussions | per-repo discussion window |
| `reddit_rss` | Reddit subreddit RSS | **off by default**; anonymous RSS is often rate-limited |

Adding a source is one adapter function; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Quick start

Requires Python 3.11+.

```bash
git clone https://github.com/boowenchen/zh-community-radar.git
cd zh-community-radar
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"            # or: pip install -r requirements-runtime.lock && pip install -e .

cp config/sources.example.json config/sources.local.json
radar capabilities
radar collect --due --max-requests 50 --max-seconds 120
radar events rebuild
radar events list --limit 5
radar ask "What are people saying about llama.cpp?"
```

`config/*.local.json`, `var/` and databases are git-ignored. Data goes to `var/radar` unless you
pass `--data-dir` or set `RADAR_DATA_DIR`.

### Optional: model analysis

```bash
# Claude Code CLI (uses your own subscription; tools, MCP and plugins are disabled for the call)
cp config/model.claude_cli.example.json config/model.local.json

# or any OpenAI-compatible endpoint (LiteLLM, vLLM, Ollama, a hosted API ...)
cp config/model.openai_compatible.example.json config/model.local.json
export RADAR_LLM_API_KEY=...       # the variable name is set in http.api_key_env

radar analyze --limit 4
radar digest build --limit 8
radar usage --scope day
```

Plain `http://` endpoints are only accepted for a fixed loopback or private IP literal and only
with `"allow_private_http": true`. Everything else must be HTTPS. API keys are read from the
environment at call time and never stored in config files.

## CLI

Global options go **before** the command: `radar [--data-dir DIR] [--config FILE] [--ledger-dir DIR]
[--run-id ID] [--idempotency-key KEY] <command>`.

| Command | What it does |
|---|---|
| `capabilities` | version, operations and adapters |
| `collect --due --sources a,b --max-requests N --max-seconds S` | fetch due sources; writes items, versions and checkpoints; no model calls |
| `sources status` / `items --limit N` | source health and raw items |
| `changes --after CURSOR` | change feed with `next_cursor` |
| `events rebuild` / `events list` / `events show --event-id ID` | build and query events |
| `analyze --model-config FILE --limit N` | budgeted model analysis with quote checks |
| `digest build --since ISO --cutoff ISO --limit N` | immutable Markdown/JSON digest |
| `ask "question"` | deterministic local query with evidence and gaps |
| `feedback add --target-id ID --label useful` | record reader feedback |
| `usage --scope run\|day\|month` | model usage, observed vs reserved |
| `outbox` | delivery queue state |
| `backup DIR` / `verify-snapshot DIR` / `restore SNAPSHOT NEWDIR` | snapshots |

Output is one JSON object: `status` is `success`, `partial`, `failed` or `skipped` (exit code 0, 2,
1, 3). Check `status` and `errors` before reading `data`.

`python -m radar.runtime --runtime-config FILE` runs one bounded "tick" (collect → rebuild →
changes → optional analyze/digest) for use from cron or a systemd timer. See
`deploy/runtime.example.json`; it is marked `proposal_only` and must be copied and edited first.

## Configuration

- `config/sources.example.json`: source list. Each source has `id`, `adapter`, `enabled`,
  `interval_minutes` and adapter options. A top-level `"user_agent"` (or `RADAR_USER_AGENT`)
  overrides the default `Radar/<version> (+https://github.com/boowenchen/zh-community-radar)`.
- `config/model.*.example.json`: model provider, budgets and limits.
- `.env.example`: environment variable names.

## Ethics and polite use

Radar is meant for **public** pages and APIs only. It never logs in, never uses cookies or
private tokens for sources, and identifies itself with a User-Agent that links back here.

- Keep polling slow. The example intervals are 30–180 minutes per source with small page and
  comment limits. Please do not lower them for sites you do not run.
- Respect `robots.txt`, rate limits and each site's terms. Radar honours `Retry-After` and backs
  off on 429/403 instead of retrying.
- PTT and Reddit are disabled in the example config. Check the current rules of those sites
  before you turn them on.
- Quote briefly and link to the original. Do not republish full posts or build a mirror.
- Digests describe a sample of what was fetched, not public opinion.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
pip-audit
```

`requirements-runtime.lock` and `requirements-dev.lock` pin tested versions. `pywin32` is only
installed on Windows (pulled in by `portalocker`).

## License

[MIT](LICENSE)
