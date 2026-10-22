"""Advisory legacy preview and opt-in, recoverable managed HTTP hot cache.

The project does not yet maintain an authoritative raw-artifact reference graph.
Even an expired, successful HTTP snapshot is therefore only an unverified
candidate. Managed artifacts use an internal immutable compressed archive; only
their redundant hot copies can be pruned. Source text/references are untouched.
"""

import gzip
import hashlib
import json
import re
import sqlite3
import stat
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import portalocker

from .models import utcnow

POLICY_DAYS = {"post": 30, "comment": 14, "mixed": 14}
PROTECTED_PARTS = {
    "validation",
    "fixtures",
    "frozen",
    "holdout",
    "acceptance",
    "checkpoints",
    "reports",
    "backups",
}


def _instant(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("retention_time_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("retention_time_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("retention_time_invalid")
    return parsed.astimezone(UTC)


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_parts(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        return None
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if ":" in normalized or "\x00" in normalized or any(part in {"", ".", ".."} for part in parts):
        return None
    # Refuse Windows normalization aliases and device paths on every platform.
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
    if any(part.endswith((" ", ".")) or part.split(".")[0].casefold() in reserved for part in parts):
        return None
    return parts


def _entry(root, row, now, root_protected):
    parts = _safe_parts(row.get("path"))
    entry = {"path": "/".join(parts) if parts else None, "classification": "blocked"}

    def finish(classification, reason):
        entry.update(classification=classification, reason=reason)
        return entry

    if not parts:
        return finish("blocked", "unsafe_relative_path")
    folded = [part.casefold() for part in parts]
    filename = folded[-1].removesuffix("-wal").removesuffix("-shm").removesuffix("-journal")
    if (
        root_protected
        or PROTECTED_PARTS.intersection(folded)
        or filename.endswith((".db", ".sqlite", ".sqlite3"))
    ):
        return finish("protected", "evidence_or_frozen_path")
    if folded[0] != "raw":
        return finish("protected", "outside_raw_scope")
    target = root
    try:
        for index, part in enumerate(parts):
            target = target / part
            info = target.lstat()
            if _is_link(info):
                return finish("blocked", "symlink_or_reparse_point")
            if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                return finish("blocked", "not_regular_file")
        if not stat.S_ISREG(info.st_mode):
            return finish("blocked", "not_regular_file")
    except OSError:
        return finish("blocked", "file_unavailable")
    entry["size_bytes"] = info.st_size
    if row.get("artifact_kind") != "http_snapshot":
        return finish(
            "blocked",
            "log_policy_unspecified" if row.get("artifact_kind") == "log" else "unknown_artifact_kind",
        )
    if (
        type(row.get("http_status")) is not int
        or row["http_status"] != 200
        or row.get("parse_status") != "success"
    ):
        return finish("protected", "snapshot_not_verified_success")
    kind = row.get("content_kind")
    if not isinstance(kind, str) or kind not in POLICY_DAYS:
        return finish("blocked", "unknown_content_kind")
    entry["policy_days"] = POLICY_DAYS[kind]
    try:
        observed = _instant(row.get("observed_at"))
    except ValueError:
        return finish("blocked", "observation_time_invalid")
    age_seconds = (now - observed).total_seconds()
    if age_seconds < 0:
        return finish("blocked", "observation_time_in_future")
    entry["age_days"] = round(age_seconds / 86400, 4)
    if age_seconds < POLICY_DAYS[kind] * 86400:
        return finish("retain", "within_retention_window")
    return finish("expired_unverified_candidate", "reference_closure_unavailable")


def plan_retention(data_dir, inventory=None, *, now=None):
    """Classify at most 1,000 explicitly described files, without filesystem writes.

    Caller-provided metadata is advisory, not proof of content type, successful
    parsing, absence of references, or permission to archive. No action list can
    be generated until a complete artifact catalog/reference closure exists.
    """
    if inventory is None:
        inventory = []
    if (
        not isinstance(inventory, list)
        or len(inventory) > 1000
        or any(not isinstance(row, dict) for row in inventory)
    ):
        raise ValueError("retention_inventory_invalid")
    root = Path(data_dir).absolute()
    try:
        for parent in (*reversed(root.parents), root):
            info = parent.lstat()
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("retention_root_invalid")
    except OSError as exc:
        raise ValueError("retention_root_invalid") from exc
    root_protected = bool(PROTECTED_PARTS.intersection(part.casefold() for part in root.parts))
    instant = _instant(utcnow() if now is None else now)
    return {
        "schema_version": 1,
        "mode": "preview_only",
        "archive_implemented": False,
        "deletion_implemented": False,
        "actions": [],
        "blockers": [
            "authoritative_artifact_catalog_unavailable",
            "reference_closure_unavailable",
            "log_policy_unspecified",
        ],
        "inventory_status": "advisory_only" if inventory else "not_supplied_or_empty",
        "as_of": instant.isoformat().replace("+00:00", "Z"),
        "entries": [_entry(root, row, instant, root_protected) for row in inventory],
    }


RAW_LIMIT = 4_000_000
ARCHIVE_LIMIT = 256 * 1024 * 1024
RUN_LIMIT = 32 * 1024 * 1024


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _artifact_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("raw_artifact_id_invalid")
    return value


class ManagedRawCache:
    """Internal archive + disposable hot file, backed up by the existing Store.

    No existing file can be adopted. The archive is stored in additive versioned
    tables, so Store.backup and recovery retain it without a second backup system.
    The local file lock coordinates this API only, not writers on another host.
    """

    def __init__(self, db, *, max_archive_bytes=ARCHIVE_LIMIT):
        if type(max_archive_bytes) is not int or not 1 <= max_archive_bytes <= ARCHIVE_LIMIT:
            raise ValueError("raw_archive_budget_invalid")
        self.db, self.conn = db, db.conn
        self.root = Path(db.data_dir).absolute()
        self.max_archive_bytes = max_archive_bytes
        with self._locked():
            self.conn.execute("""CREATE TABLE IF NOT EXISTS raw_cache_blobs_v1 (
                sha256 TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL,
                compressed_sha256 TEXT NOT NULL, payload BLOB NOT NULL,
                hot_owned INTEGER NOT NULL DEFAULT 0)""")
            if "hot_owned" not in {
                row[1] for row in self.conn.execute("PRAGMA table_info(raw_cache_blobs_v1)")
            }:
                # Older managed catalogs do not prove ownership of a filesystem copy.
                self.conn.execute(
                    "ALTER TABLE raw_cache_blobs_v1 ADD COLUMN hot_owned INTEGER NOT NULL DEFAULT 0"
                )
            self.conn.execute("""CREATE TABLE IF NOT EXISTS raw_cache_artifacts_v1 (
                artifact_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                manifest TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
                parse_status TEXT NOT NULL DEFAULT 'unknown', pin_reason TEXT,
                pruned_at TEXT, FOREIGN KEY (sha256) REFERENCES raw_cache_blobs_v1(sha256))""")
            self.conn.execute("CREATE INDEX IF NOT EXISTS raw_cache_sha_v1 ON raw_cache_artifacts_v1(sha256)")
            self.conn.commit()

    def _path(self, relative, *, directory=False, create=False):
        """Check every existing ancestor, including Windows junctions, before use."""
        target = self.root
        for parent in (*reversed(target.parents), target):
            info = parent.lstat()
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("raw_path_unsafe")
        parts = _safe_parts(relative)
        if not parts:
            raise ValueError("raw_path_unsafe")
        for index, part in enumerate(parts):
            target = target / part
            is_dir = index < len(parts) - 1 or directory
            try:
                info = target.lstat()
            except FileNotFoundError:
                if is_dir and create:
                    target.mkdir()
                    info = target.lstat()
                elif index == len(parts) - 1 and not is_dir:
                    return target
                else:
                    raise ValueError("raw_path_unavailable") from None
            if _is_link(info) or not (stat.S_ISDIR(info.st_mode) if is_dir else stat.S_ISREG(info.st_mode)):
                raise ValueError("raw_path_unsafe")
        return target

    @contextmanager
    def _locked(self):
        if self.conn.in_transaction:
            raise ValueError("raw_ambient_transaction")
        path = self._path(".raw-cache.lock")
        with portalocker.Lock(path, timeout=1):
            self._path(".raw-cache.lock")
            try:
                yield
            except BaseException:
                self.conn.rollback()
                raise

    def _hot(self, sha256, *, create=False):
        _artifact_id(sha256)
        self._path("raw/managed", directory=True, create=create)
        return self._path(f"raw/managed/{sha256}.raw")

    def _row(self, artifact_id):
        row = self.conn.execute(
            "SELECT * FROM raw_cache_artifacts_v1 WHERE artifact_id=?", (_artifact_id(artifact_id),)
        ).fetchone()
        if row is None:
            raise ValueError("raw_artifact_unknown")
        if _sha(row["manifest"].encode()) != row["manifest_sha256"]:
            raise ValueError("raw_manifest_hash_mismatch")
        value = json.loads(row["manifest"])
        if _sha(_json(value).encode()) != artifact_id or value["sha256"] != row["sha256"]:
            raise ValueError("raw_manifest_hash_mismatch")
        return row, value

    def _archive(self, sha256):
        row = self.conn.execute("SELECT * FROM raw_cache_blobs_v1 WHERE sha256=?", (sha256,)).fetchone()
        if row is None or not 0 <= row["size_bytes"] <= RAW_LIMIT:
            raise ValueError("raw_archive_invalid")
        payload = row["payload"]
        if not isinstance(payload, bytes) or len(payload) > RAW_LIMIT + 4096:
            raise ValueError("raw_archive_invalid")
        if _sha(payload) != row["compressed_sha256"]:
            raise ValueError("raw_archive_hash_mismatch")
        try:
            reader = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = reader.decompress(payload, row["size_bytes"] + 1)
        except zlib.error:
            raise ValueError("raw_archive_invalid") from None
        if (
            len(body) != row["size_bytes"]
            or not reader.eof
            or reader.unused_data
            or reader.unconsumed_tail
            or _sha(body) != sha256
        ):
            raise ValueError("raw_archive_hash_mismatch")
        return body

    def capture(self, body, *, source_id, observed_at=None, content_kind="mixed", archive_policy="unknown"):
        if not isinstance(body, bytes) or len(body) > RAW_LIMIT:
            raise ValueError("raw_body_invalid")
        if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", source_id):
            raise ValueError("raw_source_invalid")
        if content_kind not in POLICY_DAYS or archive_policy not in {"unknown", "archive_allowed"}:
            raise ValueError("raw_policy_invalid")
        observed = _instant(observed_at or utcnow()).isoformat().replace("+00:00", "Z")
        sha256 = _sha(body)
        manifest = _json(
            {
                "version": 1,
                "source_id": source_id,
                "observed_at": observed,
                "content_kind": content_kind,
                "archive_policy": archive_policy,
                "sha256": sha256,
                "size_bytes": len(body),
            }
        )
        artifact_id = _sha(manifest.encode())
        compressed = gzip.compress(body, mtime=0)
        with self._locked():
            self.conn.execute("BEGIN IMMEDIATE")
            exists = self.conn.execute(
                "SELECT 1 FROM raw_cache_blobs_v1 WHERE sha256=?", (sha256,)
            ).fetchone()
            if not exists:
                size = self.conn.execute(
                    "SELECT coalesce(sum(length(payload)),0) FROM raw_cache_blobs_v1"
                ).fetchone()[0]
                if size + len(compressed) > self.max_archive_bytes:
                    raise ValueError("raw_archive_budget_exhausted")
                self.conn.execute(
                    "INSERT INTO raw_cache_blobs_v1 (sha256,size_bytes,compressed_sha256,payload) VALUES (?,?,?,?)",
                    (sha256, len(body), _sha(compressed), compressed),
                )
            self._archive(sha256)  # Verify decompressed bytes, not just a gzip-file hash.
            exists = self.conn.execute(
                "SELECT 1 FROM raw_cache_artifacts_v1 WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            if not exists:
                if self.conn.execute("SELECT count(*) FROM raw_cache_artifacts_v1").fetchone()[0] >= 10000:
                    raise ValueError("raw_catalog_budget_exhausted")
                self.conn.execute(
                    "INSERT INTO raw_cache_artifacts_v1 (artifact_id,sha256,manifest,manifest_sha256) VALUES (?,?,?,?)",
                    (artifact_id, sha256, manifest, artifact_id),
                )
            self.conn.commit()  # Archive is durable before the optional redundant hot copy.
            hot = self._hot(sha256, create=True)
            if not hot.exists():
                # Bound total uncompressed hot capacity as well as archive bytes.
                total = self.conn.execute(
                    "SELECT coalesce(sum(size_bytes),0) FROM raw_cache_blobs_v1"
                ).fetchone()[0]
                if total <= ARCHIVE_LIMIT:
                    with hot.open("xb") as stream:
                        stream.write(body)
                    with self.conn:
                        self.conn.execute(
                            "UPDATE raw_cache_blobs_v1 SET hot_owned=1 WHERE sha256=?", (sha256,)
                        )
            elif not self.conn.execute(
                "SELECT hot_owned FROM raw_cache_blobs_v1 WHERE sha256=?", (sha256,)
            ).fetchone()[0]:
                raise ValueError("raw_hot_unmanaged")
            elif hot.stat().st_size != len(body) or _sha(hot.read_bytes()) != sha256:
                raise ValueError("raw_hot_hash_mismatch")
            return {
                "artifact_id": artifact_id,
                "sha256": sha256,
                "size_bytes": len(body),
                "archive_verified": True,
                "hot_available": hot.exists(),
            }

    def finish(self, artifact_ids, *, parse_success):
        if type(parse_success) is not bool:
            raise ValueError("raw_parse_status_invalid")
        ids = self._ids(artifact_ids)
        with self._locked(), self.conn:
            for artifact_id in ids:
                self._row(artifact_id)
                self.conn.execute(
                    "UPDATE raw_cache_artifacts_v1 SET parse_status=? WHERE artifact_id=? AND parse_status='unknown'",
                    ("success" if parse_success else "failed", artifact_id),
                )

    def pin(self, artifact_id, *, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError("raw_pin_reason_required")
        with self._locked():
            self._row(artifact_id)
            with self.conn:
                self.conn.execute(
                    "UPDATE raw_cache_artifacts_v1 SET pin_reason=? WHERE artifact_id=?",
                    (reason, artifact_id),
                )

    def resolve(self, artifact_id):
        with self._locked():
            row, manifest = self._row(artifact_id)
            body = self._archive(row["sha256"])
            if len(body) != manifest["size_bytes"]:
                raise ValueError("raw_manifest_hash_mismatch")
            return body  # Canonical archive makes hot-file absence harmless after restore.

    @staticmethod
    def _ids(artifact_ids):
        if not isinstance(artifact_ids, list) or len(artifact_ids) > 100:
            raise ValueError("raw_batch_invalid")
        return list(dict.fromkeys(_artifact_id(value) for value in artifact_ids))

    def _eligibility(self, sha256, instant):
        if PROTECTED_PARTS.intersection(part.casefold() for part in self.root.parts):
            return "protected_root"
        # Every managed alias to this shared hot file must permit removal.
        ids = self.conn.execute(
            "SELECT artifact_id FROM raw_cache_artifacts_v1 WHERE sha256=?", (sha256,)
        ).fetchall()
        for (artifact_id,) in ids:
            row, value = self._row(artifact_id)
            if row["pin_reason"]:
                return "pinned"
            if row["parse_status"] != "success":
                return "parse_not_verified"
            if value["archive_policy"] != "archive_allowed":
                return "archive_policy_unknown"
            age = (instant - _instant(value["observed_at"])).total_seconds()
            if age < 0:
                return "observation_time_in_future"
            if age < POLICY_DAYS[value["content_kind"]] * 86400:
                return "within_retention_window"
        return None

    def prune(self, artifact_ids, *, now=None, max_files=20, max_bytes=RUN_LIMIT):
        ids = self._ids(artifact_ids)
        if (
            type(max_files) is not int
            or not 1 <= max_files <= 100
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= RUN_LIMIT
        ):
            raise ValueError("raw_batch_invalid")
        instant = _instant(now or utcnow())
        result = {
            "scope": "managed_hot_cache_only",
            "pruned_files": 0,
            "pruned_bytes": 0,
            "entries": [],
            "remaining": [],
        }
        with self._locked():
            for index, artifact_id in enumerate(ids):
                if result["pruned_files"] >= max_files:
                    result["remaining"] = ids[index:]
                    break
                try:
                    row, manifest = self._row(artifact_id)
                    reason = self._eligibility(row["sha256"], instant)
                    if reason:
                        result["entries"].append({"artifact_id": artifact_id, "reason": reason})
                        continue
                    body = self._archive(row["sha256"])
                    if len(body) != manifest["size_bytes"]:
                        raise ValueError("raw_manifest_hash_mismatch")
                    if result["pruned_bytes"] + len(body) > max_bytes:
                        result["remaining"].append(artifact_id)
                        continue
                    try:
                        hot = self._hot(row["sha256"])
                    except ValueError as exc:
                        if str(exc) != "raw_path_unavailable":
                            raise
                        hot = None  # Restored archive: hot tree deliberately not in backup.
                    reason = "archive_only"
                    if hot and hot.exists():
                        if not self.conn.execute(
                            "SELECT hot_owned FROM raw_cache_blobs_v1 WHERE sha256=?", (row["sha256"],)
                        ).fetchone()[0]:
                            raise ValueError("raw_hot_unmanaged")
                        if hot.stat().st_size != len(body) or _sha(hot.read_bytes()) != row["sha256"]:
                            raise ValueError("raw_hot_hash_mismatch")
                        # Exact single generated file, rechecked while holding the local lock.
                        self._hot(row["sha256"])
                        hot.unlink()
                        result["pruned_files"] += 1
                        result["pruned_bytes"] += len(body)
                        reason = "hot_pruned_archive_verified"
                    with self.conn:
                        self.conn.execute(
                            "UPDATE raw_cache_blobs_v1 SET hot_owned=0 WHERE sha256=?", (row["sha256"],)
                        )
                        self.conn.execute(
                            "UPDATE raw_cache_artifacts_v1 SET pruned_at=? WHERE sha256=?",
                            (instant.isoformat(), row["sha256"]),
                        )
                    result["entries"].append({"artifact_id": artifact_id, "reason": reason})
                except (ValueError, OSError, sqlite3.Error) as exc:
                    self.conn.rollback()
                    reason = (
                        str(exc)
                        if isinstance(exc, ValueError) and str(exc).startswith("raw_")
                        else "raw_io_unavailable"
                    )
                    result["entries"].append({"artifact_id": artifact_id, "reason": reason})
        return result


class RawSnapshotObserver:
    """One per collect run: shared budget, source-local receipts, no URLs/headers."""

    def __init__(
        self,
        cache,
        *,
        max_files=100,
        max_bytes=RUN_LIMIT,
        max_source_files=10,
        max_source_bytes=8 * 1024 * 1024,
    ):
        if (
            type(max_files) is not int
            or not 1 <= max_files <= 100
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= RUN_LIMIT
        ):
            raise ValueError("raw_batch_invalid")
        if (
            type(max_source_files) is not int
            or not 1 <= max_source_files <= 100
            or type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= RUN_LIMIT
        ):
            raise ValueError("raw_batch_invalid")
        self.cache, self.max_files, self.max_bytes = cache, max_files, max_bytes
        self.max_source_files, self.max_source_bytes = max_source_files, max_source_bytes
        self.files = self.bytes = 0
        self.start_source("unassigned")

    def start_source(self, source_id, *, content_kind="mixed", archive_policy="unknown"):
        self.source_id, self.content_kind, self.archive_policy = source_id, content_kind, archive_policy
        self.ids, self.warnings = [], []
        self.source_files = self.source_bytes = 0

    def __call__(self, body, receipt):
        if receipt.get("status") != 200:
            return
        try:
            if (
                self.source_files >= self.max_source_files
                or self.source_bytes + len(body) > self.max_source_bytes
            ):
                raise ValueError("raw_source_budget_exhausted")
            if self.files >= self.max_files or self.bytes + len(body) > self.max_bytes:
                raise ValueError("raw_run_budget_exhausted")
            self.files += 1
            self.bytes += len(body)
            self.source_files += 1
            self.source_bytes += len(body)
            value = self.cache.capture(
                body,
                source_id=self.source_id,
                content_kind=self.content_kind,
                archive_policy=self.archive_policy,
            )
            self.ids.append(value["artifact_id"])
        except (ValueError, OSError, sqlite3.Error, portalocker.exceptions.LockException) as exc:
            reason = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc).startswith("raw_")
                else "raw_io_unavailable"
            )
            self.warnings.append("raw_capture_unavailable:" + reason)

    def finish(self, *, parse_success):
        try:
            self.cache.finish(self.ids, parse_success=parse_success)
        except (ValueError, OSError, sqlite3.Error, portalocker.exceptions.LockException):
            self.warnings.append("raw_capture_unavailable:raw_parse_confirmation_failed")
        return {
            "artifact_ids": list(dict.fromkeys(self.ids)),
            "warnings": list(dict.fromkeys(self.warnings)),
            "scope": "managed_hot_cache_only",
            "archive_in_store_backup": True,
        }
