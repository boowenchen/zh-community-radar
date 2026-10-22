import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalized_time(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_id(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("invalid_public_url")
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(query), ""))


@dataclass
class Item:
    source_id: str
    native_id: str
    url: str
    title: str
    text: str
    published_at: str | None = None
    source_updated_at: str | None = None
    fetched_at: str = field(default_factory=utcnow)
    kind: str = "post"
    parent_id: str | None = None
    author: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def item_id(self):
        return stable_id(self.source_id, self.native_id)

    @property
    def content_hash(self):
        return hashlib.sha256(json_text([self.title, self.text, self.url, self.kind]).encode()).hexdigest()

    def validate(self):
        if not self.source_id or not self.native_id or not isinstance(self.text, str):
            raise ValueError("invalid_item")
        canonical_url(self.url)
        if len(self.text) > 250_000 or len(self.title) > 2000:
            raise ValueError("item_too_large")
        for field_name in ("published_at", "source_updated_at", "fetched_at"):
            value = getattr(self, field_name)
            if value is not None:
                setattr(self, field_name, normalized_time(value))
        self.metadata["timestamp_storage_precision_seconds"] = 1
        if self.kind == "comment":
            self.author = None
        return self

    def as_dict(self):
        return asdict(self)


@dataclass
class Scan:
    source_id: str
    items: list[Item] = field(default_factory=list)
    status: str = "ok"
    complete: bool = False
    checkpoint: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    requests: int = 0
    capabilities: dict = field(default_factory=dict)
    observed_at: str = field(default_factory=utcnow)
    host: str = "local"
    requested_egress: str = "direct"
    effective_egress: str = "direct"
    retry_at: str | None = None
