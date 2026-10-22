from datetime import UTC, datetime

import httpx
import pytest

from radar.fetch import Fetcher, FetchError, retry_delay


def test_retry_after_accepts_seconds_date_and_invalid_fallback():
    now = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
    assert retry_delay("120", now=now) == 120
    assert retry_delay("Thu, 22 Oct 2026 00:03:00 GMT", now=now) == 180
    assert retry_delay("not a date", now=now) == 900
    assert retry_delay("0", now=now) == 60


def test_rate_limit_blocks_same_host_without_blocking_other_hosts():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if request.url.host == "limited.example":
            return httpx.Response(429, headers={"Retry-After": "600"})
        return httpx.Response(200, content=b"ok")

    with Fetcher({"limited.example", "other.example"}, transport=httpx.MockTransport(respond)) as fetcher:
        with pytest.raises(FetchError) as first:
            fetcher.get("https://limited.example/a")
        assert first.value.retry_after_seconds == 600
        assert first.value.retry_at.endswith("Z")
        with pytest.raises(FetchError, match="host_backoff"):
            fetcher.get("https://limited.example/b")
        assert fetcher.get("https://other.example/a")[0] == b"ok"
        assert len(calls) == 2
        assert fetcher.receipts[0]["retry_after_seconds"] == 600


def test_snapshot_observer_receives_only_successful_body_and_safe_receipt():
    snapshots = []
    with Fetcher(
        {"example.com"},
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"public", headers={"Set-Cookie": "secret"})
        ),
        response_observer=lambda body, receipt: snapshots.append((body, receipt)),
    ) as fetcher:
        fetcher.get("https://example.com")
    assert snapshots[0][0] == b"public"
    assert "secret" not in str(snapshots)
    assert len(snapshots[0][1]["body_sha256"]) == 64


def test_default_user_agent_identifies_project_and_is_configurable():
    from radar import __version__
    from radar.fetch import DEFAULT_USER_AGENT, user_agent

    assert DEFAULT_USER_AGENT == f"Radar/{__version__} (+https://github.com/boowenchen/zh-community-radar)"
    seen = []

    def respond(request):
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, text="ok")

    with Fetcher({"ua.example"}, transport=httpx.MockTransport(respond), user_agent_value="MyRadar/1.0") as f:
        f.get("https://ua.example/")
    with Fetcher({"ua.example"}, transport=httpx.MockTransport(respond)) as f:
        f.get("https://ua.example/")
    assert seen == ["MyRadar/1.0", DEFAULT_USER_AGENT]
    for bad in ("", "x" * 201, "bad\nheader"):
        with pytest.raises(ValueError, match="user_agent_invalid"):
            user_agent(bad)
