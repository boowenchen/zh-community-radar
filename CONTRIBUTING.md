# Contributing

Thanks for helping. Issues and pull requests in English or Traditional Chinese are both welcome.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

CI runs the same checks on Ubuntu and Windows with Python 3.11–3.13, plus `pip-audit` and
`gitleaks`. Please keep pull requests small and focused, and add a test for every behaviour change.

## Ground rules

- Tests must not touch the network. Use `httpx.MockTransport` and small fixtures in
  `tests/fixtures/` (trim real responses to the minimum and remove usernames you do not need).
- Never commit real data directories, databases, API keys or `config/*.local.json`.
- Keep sources polite: public endpoints only, no login, no cookie reuse, conservative defaults.
- Fail closed. If something is unknown (time, coverage, usage), record it as unknown rather than
  guessing or reporting zero.

## Adding a source adapter

An adapter is a function in `src/radar/sources.py`:

```python
def mysite_scan(config, fetcher, checkpoint, known_ids) -> Scan: ...
```

- `config` is the source entry from the sources JSON (`id`, `adapter`, `url`, your options).
- `fetcher` is the bounded HTTP client. Call `fetcher.get(url, headers=...)`, which returns
  `(body_bytes, headers, status)`. It enforces the host allowlist, request/time/size caps and
  `Retry-After`; do not create your own HTTP client.
- `checkpoint` is the dict you returned last time (ETag, cursor, head IDs ...).
- `known_ids` are native IDs already stored for this source, useful to skip refetching.

Return a `Scan` (see `src/radar/models.py`) with:

- `items`: a list of `Item` objects. Posts use `kind="post"`; comments use `kind="comment"` with
  `parent_id` and `metadata["root_id"]`. Comment authors are dropped on purpose.
- `checkpoint`: what the next run needs.
- `complete`: `True` only when you fetched everything you intended to; otherwise add a warning
  explaining what was skipped.
- `capabilities`: e.g. `{"posts": True, "comments": True, "scope": "..."}`.

Then:

1. Register it in `ADAPTERS` at the bottom of `sources.py`.
2. Add an example entry (with `"enabled": false` if the site's rules are unclear) to
   `config/sources.example.json`, including `allowed_hosts` and a sensible `interval_minutes`.
3. Add tests in `tests/test_sources.py` covering a normal page, an empty page, a malformed
   payload, and a 429 with `Retry-After`.
4. Add the source to the tables in both READMEs.

## Commit messages

Short imperative subject (`Add V2EX node filter`), body if the reason is not obvious.
