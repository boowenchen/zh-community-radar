"""Bounded public-source HTTP only. Model/delivery credentials never enter this client."""

import hashlib
import json
import math
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from . import __version__

PROJECT_URL = "https://github.com/boowenchen/zh-community-radar"
DEFAULT_USER_AGENT = f"Radar/{__version__} (+{PROJECT_URL})"


def user_agent(value=None):
    """Return an operator-set User-Agent, or the default that identifies the project."""
    if value is None:
        return DEFAULT_USER_AGENT
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("user_agent_invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("user_agent_invalid")
    return value.strip()


def retry_delay(value, *, now=None, default=900):
    """Respect both Retry-After forms without sleeping or shortening server waits."""
    now = now or datetime.now(UTC)
    try:
        delay = int(value)
    except (ValueError, TypeError):
        try:
            when = parsedate_to_datetime(value)
            delay = math.ceil((when - now).total_seconds())
        except (ValueError, TypeError, OverflowError):
            delay = default
    # Even a zero/past date must not create a busy retry loop.
    return max(60, delay)


class FetchError(Exception):
    def __init__(self, code, status="network_error", retry_after_seconds=None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        self.retry_at = None
        if retry_after_seconds is not None:
            try:
                when = datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
            except OverflowError:
                when = datetime.max.replace(tzinfo=UTC)
            self.retry_at = when.isoformat(timespec="seconds").replace("+00:00", "Z")


class Fetcher:
    def __init__(
        self,
        allowed_hosts,
        max_requests=200,
        max_seconds=240,
        max_bytes=4_000_000,
        transport=None,
        response_observer=None,
        user_agent_value=None,
    ):
        self.allowed_hosts = set(allowed_hosts)
        self.max_requests = max_requests
        self.deadline = time.monotonic() + max_seconds
        self.max_bytes = max_bytes
        self.requests = 0
        self.receipts = []
        self.blocked_until = {}
        # Optional local validation hook. It must deidentify any persisted reference;
        # ordinary collection never persists raw responses or response cookies.
        self.response_observer = response_observer
        self.client = httpx.Client(
            timeout=12,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": user_agent(user_agent_value)},
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def get(self, url, *, headers=None):
        for redirect in range(4):
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or parts.hostname not in self.allowed_hosts
                or parts.username
                or parts.password
                or parts.port not in (None, 443)
            ):
                raise FetchError("unsafe_url", "blocked")
            if self.blocked_until.get(parts.hostname, 0) > time.monotonic():
                remaining_wait = math.ceil(self.blocked_until[parts.hostname] - time.monotonic())
                raise FetchError("host_backoff", "rate_limited", remaining_wait)
            if self.requests >= self.max_requests:
                raise FetchError("request_budget", "partial")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("deadline", "partial")
            self.requests += 1
            started = time.monotonic()
            try:
                with self.client.stream("GET", url, headers=headers, timeout=min(12, remaining)) as response:
                    receipt = {
                        "url": url,
                        "status": response.status_code,
                        "seconds": round(time.monotonic() - started, 3),
                        "age": response.headers.get("age"),
                        "etag": response.headers.get("etag"),
                        "last_modified": response.headers.get("last-modified"),
                        "retry_after": response.headers.get("retry-after"),
                    }
                    self.receipts.append(receipt)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if response.status_code >= 400:
                        status = {401: "auth_required", 403: "blocked", 429: "rate_limited"}.get(
                            response.status_code, "network_error"
                        )
                        delay = None
                        if response.status_code == 429 or (
                            response.status_code == 503 and response.headers.get("retry-after")
                        ):
                            delay = retry_delay(response.headers.get("retry-after"))
                            self.blocked_until[parts.hostname] = time.monotonic() + delay
                            receipt["retry_after_seconds"] = delay
                        error = FetchError(f"http_{response.status_code}", status, delay)
                        if error.retry_at:
                            receipt["retry_at"] = error.retry_at
                        raise error
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() >= self.deadline:
                            raise FetchError("deadline", "partial")
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            raise FetchError("response_too_large", "partial")
                    receipt["bytes"] = len(body)
                    receipt["body_sha256"] = hashlib.sha256(body).hexdigest()
                    if self.response_observer and response.status_code == 200:
                        self.response_observer(bytes(body), dict(receipt))
                    return bytes(body), dict(response.headers), response.status_code
            except httpx.HTTPError as error:
                raise FetchError(type(error).__name__) from None
        raise FetchError("redirect_limit", "blocked")

    def json(self, url):
        body, _, _ = self.get(url)
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise FetchError("invalid_json", "parse_error") from None
