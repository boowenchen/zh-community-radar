"""Transactional single-writer store. Checkpoints only advance after complete scans."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from . import __version__
from .models import Scan, json_text, normalized_time, stable_id, utcnow

SCHEMA_VERSION = 4


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def program_identity():
    """Identify package source bytes without assuming a checkout or invoking Git."""
    root = Path(__file__).resolve().parent
    entries = []
    for path in sorted(root.rglob("*.py")):
        if path.is_symlink() or len(entries) >= 500:
            raise ValueError("program_identity_unavailable")
        entries.append((path.relative_to(root).as_posix(), _file_hash(path)))
    return {
        "package": "zh-community-radar",
        "version": __version__,
        "source_sha256": hashlib.sha256(json_text(entries).encode()).hexdigest(),
        "source_basis": "package_python_files",
    }


def config_fingerprints(config_paths):
    """Hash explicitly supplied nonsecret configs; never copy their content/path."""
    if config_paths is None:
        return {}
    if not isinstance(config_paths, dict) or len(config_paths) > 64:
        raise ValueError("backup_config_invalid")
    result = {}
    for label, value in config_paths.items():
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label):
            raise ValueError("backup_config_invalid")
        try:
            path = Path(value)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
                raise ValueError("backup_config_invalid")
            result[label] = _file_hash(path)
        except (TypeError, OSError) as exc:
            raise ValueError("backup_config_invalid") from exc
    return dict(sorted(result.items()))


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
 item_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, native_id TEXT NOT NULL,
 url TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL,
 parent_id TEXT, author TEXT, published_at TEXT, source_updated_at TEXT,
 first_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
 content_hash TEXT NOT NULL, revision INTEGER NOT NULL, metadata TEXT NOT NULL,
 UNIQUE(source_id, native_id)
);
CREATE INDEX IF NOT EXISTS items_source ON items(source_id, fetched_at);
CREATE TABLE IF NOT EXISTS item_versions (
 item_id TEXT NOT NULL REFERENCES items(item_id), revision INTEGER NOT NULL,
 content_hash TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
 observed_at TEXT NOT NULL, PRIMARY KEY(item_id, revision)
);
CREATE TABLE IF NOT EXISTS sources (
 source_id TEXT PRIMARY KEY, status TEXT NOT NULL, last_attempt_at TEXT NOT NULL,
 last_success_at TEXT, error TEXT, checkpoint TEXT NOT NULL,
 capabilities TEXT NOT NULL, warnings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_runs (
 run_id TEXT NOT NULL, source_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 status TEXT NOT NULL, complete INTEGER NOT NULL, receipt TEXT NOT NULL,
 PRIMARY KEY(run_id, source_id)
);
CREATE TABLE IF NOT EXISTS changes (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
 revision INTEGER NOT NULL, observed_at TEXT NOT NULL,
 UNIQUE(item_id, revision)
);
CREATE TABLE IF NOT EXISTS evidence (
 evidence_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, revision INTEGER NOT NULL,
 content_hash TEXT NOT NULL, quote TEXT NOT NULL, start_offset INTEGER NOT NULL,
 end_offset INTEGER NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(item_id, revision) REFERENCES item_versions(item_id, revision)
);
CREATE TABLE IF NOT EXISTS receipts (
 idempotency_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
 receipt TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, anchor TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 revision INTEGER NOT NULL, fingerprint TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, score INTEGER NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_members (
 item_id TEXT PRIMARY KEY REFERENCES items(item_id), event_id TEXT NOT NULL REFERENCES events(event_id)
);
CREATE INDEX IF NOT EXISTS members_event ON event_members(event_id);
CREATE TABLE IF NOT EXISTS event_versions (
 event_id TEXT NOT NULL REFERENCES events(event_id), revision INTEGER NOT NULL,
 observed_at TEXT NOT NULL, snapshot TEXT NOT NULL, PRIMARY KEY(event_id,revision)
);
CREATE TABLE IF NOT EXISTS item_context_versions (
 item_id TEXT NOT NULL, revision INTEGER NOT NULL, context TEXT NOT NULL,
 PRIMARY KEY(item_id, revision),
 FOREIGN KEY(item_id, revision) REFERENCES item_versions(item_id, revision)
);
CREATE INDEX IF NOT EXISTS items_parent ON items(source_id,parent_id);
CREATE TABLE IF NOT EXISTS event_item_state (
 item_id TEXT PRIMARY KEY REFERENCES items(item_id), revision INTEGER NOT NULL,
 source_id TEXT NOT NULL, native_id TEXT NOT NULL, kind TEXT NOT NULL,
 parent_id TEXT, root_id TEXT, observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_state_native ON event_item_state(source_id,native_id);
CREATE INDEX IF NOT EXISTS event_state_root ON event_item_state(source_id,root_id);
CREATE INDEX IF NOT EXISTS event_state_parent ON event_item_state(source_id,parent_id);
"""


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "radar.sqlite3"
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, SCHEMA_VERSION):
            self.conn.close()
            raise ValueError("schema_incompatible")
        self.conn.executescript(SCHEMA)
        if "next_allowed_at" not in {r[1] for r in self.conn.execute("PRAGMA table_info(sources)")}:
            self.conn.execute("ALTER TABLE sources ADD COLUMN next_allowed_at TEXT")
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.conn.close()

    def save_scan(self, scan: Scan, run_id: str) -> dict:
        scan.observed_at = normalized_time(scan.observed_at)
        if scan.retry_at:
            scan.retry_at = normalized_time(scan.retry_at)
        if scan.complete and scan.status not in {"ok", "no_new", "stale"}:
            raise ValueError("failed_scan_cannot_be_complete")
        added = updated = 0
        with self.conn:
            for item in scan.items:
                item.validate()
                if item.source_id != scan.source_id:
                    raise ValueError("source_mismatch")
                old = self.conn.execute("SELECT * FROM items WHERE item_id=?", (item.item_id,)).fetchone()
                changed = old is None or old["content_hash"] != item.content_hash
                if old:
                    old_meta = json.loads(old["metadata"])
                    changed |= any(
                        old[k] != getattr(item, k) for k in ("published_at", "parent_id", "source_updated_at")
                    )
                    changed |= old_meta.get("external_url") != item.metadata.get("external_url")
                    semantic = ("discussion_status", "is_pull_request", "status", "root_id")
                    changed |= any(old_meta.get(k) != item.metadata.get(k) for k in semantic)
                    # A legacy row needs a new observation, not a retroactive context backfill.
                    changed |= (
                        self.conn.execute(
                            "SELECT 1 FROM item_context_versions WHERE item_id=? AND revision=?",
                            (item.item_id, old["revision"]),
                        ).fetchone()
                        is None
                    )
                rev = (old["revision"] + int(changed)) if old else 1
                first = old["first_seen_at"] if old else item.fetched_at
                values = (
                    item.item_id,
                    item.source_id,
                    item.native_id,
                    item.url,
                    item.title,
                    item.text,
                    item.kind,
                    item.parent_id,
                    item.author,
                    item.published_at,
                    item.source_updated_at,
                    first,
                    item.fetched_at,
                    item.content_hash,
                    rev,
                    json_text(item.metadata),
                )
                self.conn.execute(
                    """INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(item_id) DO UPDATE SET url=excluded.url, title=excluded.title,
                    text=excluded.text, kind=excluded.kind, parent_id=excluded.parent_id,
                    author=excluded.author, published_at=excluded.published_at,
                    source_updated_at=excluded.source_updated_at, fetched_at=excluded.fetched_at,
                    content_hash=excluded.content_hash, revision=excluded.revision, metadata=excluded.metadata""",
                    values,
                )
                if changed:
                    added += int(old is None)
                    updated += int(old is not None)
                    self.conn.execute(
                        "INSERT INTO item_versions VALUES (?,?,?,?,?,?)",
                        (item.item_id, rev, item.content_hash, item.title, item.text, item.fetched_at),
                    )
                    self.conn.execute(
                        "INSERT INTO changes(item_id,revision,observed_at) VALUES (?,?,?)",
                        (item.item_id, rev, item.fetched_at),
                    )
                    context = {
                        k: getattr(item, k)
                        for k in (
                            "source_id",
                            "native_id",
                            "url",
                            "kind",
                            "parent_id",
                            "published_at",
                            "source_updated_at",
                            "metadata",
                        )
                    }
                    context["first_seen_at"] = first
                    self.conn.execute(
                        "INSERT INTO item_context_versions VALUES (?,?,?)",
                        (item.item_id, rev, json_text(context)),
                    )
            previous = self.conn.execute(
                "SELECT * FROM sources WHERE source_id=?", (scan.source_id,)
            ).fetchone()
            checkpoint = (
                json_text(scan.checkpoint)
                if scan.complete
                else (previous["checkpoint"] if previous else "{}")
            )
            success = scan.complete and scan.status in {"ok", "no_new"}
            last_success = (
                scan.observed_at if success else (previous["last_success_at"] if previous else None)
            )
            status = scan.status
            if status == "ok":
                status = ("ok" if added + updated else "no_new") if scan.complete else "partial"
            receipt = {
                "source_id": scan.source_id,
                "status": status,
                "complete": scan.complete,
                "added": added,
                "updated": updated,
                "changed": added + updated,
                "observed": len(scan.items),
                "requests": scan.requests,
                "warnings": scan.warnings,
                "error": scan.error,
                "host": scan.host,
                "requested_egress": scan.requested_egress,
                "effective_egress": scan.effective_egress,
                "observed_at": scan.observed_at,
                "capabilities": scan.capabilities,
                "next_allowed_at": scan.retry_at,
            }
            self.conn.execute(
                """INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET status=excluded.status,last_attempt_at=excluded.last_attempt_at,
                last_success_at=excluded.last_success_at,error=excluded.error,checkpoint=excluded.checkpoint,
                capabilities=excluded.capabilities,warnings=excluded.warnings,next_allowed_at=excluded.next_allowed_at""",
                (
                    scan.source_id,
                    status,
                    scan.observed_at,
                    last_success,
                    scan.error,
                    checkpoint,
                    json_text(scan.capabilities),
                    json_text(scan.warnings),
                    scan.retry_at,
                ),
            )
            self.conn.execute(
                "INSERT INTO source_runs VALUES (?,?,?,?,?,?)",
                (run_id, scan.source_id, scan.observed_at, status, int(scan.complete), json_text(receipt)),
            )
        return receipt

    def checkpoint(self, source_id):
        row = self.conn.execute("SELECT checkpoint FROM sources WHERE source_id=?", (source_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def source_status(self):
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM sources ORDER BY source_id")]
        for row in rows:
            for key in ("checkpoint", "capabilities", "warnings"):
                row[key] = json.loads(row[key])
        return rows

    def items(self, limit=100, source_id=None, include_comments=True):
        where = []
        params = []
        if source_id:
            where.append("source_id=?")
            params.append(source_id)
        if not include_comments:
            where.append("kind!='comment'")
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = self.conn.execute(
            "SELECT * FROM items" + clause + " ORDER BY fetched_at DESC,item_id LIMIT ?",
            (*params, max(1, min(int(limit), 10000))),
        ).fetchall()
        return [dict(r) | {"metadata": json.loads(r["metadata"])} for r in rows]

    def changes(self, after=0, limit=100):
        rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)
            )
        ]
        return {
            "changed": bool(rows),
            "events": rows,
            "next_cursor": rows[-1]["seq"] if rows else after,
            "model_required": bool(rows),
            "notify": False,
        }

    def receipt(self, key, request_hash):
        row = self.conn.execute("SELECT * FROM receipts WHERE idempotency_key=?", (key,)).fetchone()
        if row and row["request_hash"] != request_hash:
            raise ValueError("idempotency_conflict")
        return json.loads(row["receipt"]) if row else None

    def save_receipt(self, key, request_hash, receipt):
        with self.conn:
            self.conn.execute(
                "INSERT INTO receipts VALUES (?,?,?,?)", (key, request_hash, json_text(receipt), utcnow())
            )

    def add_evidence(self, item_id, revision, quote):
        row = self.conn.execute(
            "SELECT * FROM item_versions WHERE item_id=? AND revision=?", (item_id, revision)
        ).fetchone()
        if not row or not quote or quote not in row["text"]:
            raise ValueError("quote_not_found")
        start = row["text"].index(quote)
        evidence = {
            "evidence_id": stable_id(item_id, str(revision), quote),
            "item_id": item_id,
            "revision": revision,
            "content_hash": row["content_hash"],
            "quote": quote,
            "start_offset": start,
            "end_offset": start + len(quote),
            "created_at": utcnow(),
        }
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?,?)", tuple(evidence.values())
            )
        return evidence

    def backup(self, destination: Path, *, config_paths=None):
        from .recovery import atomic_write_json

        configs = config_fingerprints(config_paths)
        program = program_identity()
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        output = destination / "radar.sqlite3"
        with closing(sqlite3.connect(output)) as target:
            self.conn.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            version = target.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            counts = {
                name: target.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] if name in tables else 0
                for name in ("items", "events", "reports")
            }
        if integrity != "ok":
            raise ValueError("snapshot_integrity_failed")
        if configs != config_fingerprints(config_paths) or program != program_identity():
            raise ValueError("backup_provenance_changed")
        manifest = {
            "manifest_version": 2,
            "schema_version": version,
            "created_at": utcnow(),
            "integrity": integrity,
            **counts,
            "sha256": _file_hash(output),
            "program": program,
            "config_hashes": configs,
            "config_status": "captured" if configs else "not_supplied",
            "secrets_included": False,
        }
        atomic_write_json(destination / "manifest.json", manifest, overwrite=False)
        return manifest
