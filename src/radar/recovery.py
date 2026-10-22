"""Validate sealed snapshots and restore to a new, explicitly shadow-only data directory.

The database stays byte-identical. The restore guard is separate so saved event,
checkpoint and delivery identities are never rewritten merely to restore data.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .models import utcnow
from .store import SCHEMA_VERSION, config_fingerprints, program_identity

MANIFEST_LIMIT = 65_536
GUARD_NAME = "restore-state.json"


def atomic_write_text(path, text, *, overwrite=True):
    """Flush UTF-8 to a sibling temporary file, then publish a complete file.

    The parent must exist. With overwrite=False an atomic hard link refuses an
    existing target even if another process creates it during this operation.
    """
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".radar-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path, value, *, overwrite=True):
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        overwrite=overwrite,
    )


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path, error):
    try:
        path = Path(path)
        if path.is_symlink() or path.stat().st_size > MANIFEST_LIMIT:
            raise ValueError(error)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(error)  # noqa: TRY004 - Stable data-validation error at the CLI boundary.
        return data
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(error) from exc


def _identifier(name):
    # SQLite identifiers cannot be bound parameters; quote embedded double quotes.
    return '"' + name.replace('"', '""') + '"'


def _table_profile(connection, name):
    columns = connection.execute(f"PRAGMA table_info({_identifier(name)})").fetchall()
    primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
    ordering = primary or [row[1] for row in columns]
    query = f"SELECT * FROM {_identifier(name)} ORDER BY " + ",".join(map(_identifier, ordering))
    digest = hashlib.sha256()
    digest.update(json.dumps([row[1] for row in columns], ensure_ascii=False).encode("utf-8"))
    count = 0
    for row in connection.execute(query):
        normalized = [{"blob_hex": value.hex()} if isinstance(value, bytes) else value for value in row]
        digest.update(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _provenance(manifest, config_paths, require_program_match):
    version = manifest.get("manifest_version", 1)
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("snapshot_manifest_version_unsupported")
    if version == 1:
        if config_paths is not None or require_program_match:
            raise ValueError("snapshot_provenance_missing")
        return "unknown_legacy", "unknown_legacy"
    program, configs = manifest.get("program"), manifest.get("config_hashes")
    if (
        not isinstance(program, dict)
        or program.get("package") != "zh-community-radar"
        or not isinstance(program.get("version"), str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}", str(program.get("version", "")))
        or not isinstance(program.get("source_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", program["source_sha256"])
        or program.get("source_basis") != "package_python_files"
        or not isinstance(configs, dict)
        or len(configs) > 64
        or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key)
            or not isinstance(value, str)
            or not re.fullmatch(r"[a-f0-9]{64}", value)
            for key, value in configs.items()
        )
        or manifest.get("config_status") != ("captured" if configs else "not_supplied")
        or any(type(manifest.get(key)) is not int or manifest[key] < 0 for key in ("events", "reports"))
    ):
        raise ValueError("snapshot_manifest_invalid")
    compatible = "exact" if program == program_identity() else "different"
    if require_program_match and compatible != "exact":
        raise ValueError("snapshot_program_mismatch")
    verified = "unverified" if configs else "not_supplied"
    if config_paths is not None:
        if config_fingerprints(config_paths) != configs:
            raise ValueError("snapshot_config_mismatch")
        verified = "matched" if configs else "not_supplied"
    return compatible, verified


def validate_snapshot(snapshot_dir, *, config_paths=None, require_program_match=False):
    """Read-only validation of a Store.backup directory, without migrating schema.

    Hashes protect consistency against accidental corruption, not authenticity.
    Additional application tables are retained and included in the fingerprints.
    """
    snapshot_dir = Path(snapshot_dir)
    if snapshot_dir.is_symlink():
        raise ValueError("snapshot_symlink")
    database = snapshot_dir / "radar.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise ValueError("snapshot_database_missing")
    wal = database.with_name(database.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("snapshot_has_wal")
    manifest = _read_json(snapshot_dir / "manifest.json", "snapshot_manifest_invalid")
    program_compatibility, config_verification = _provenance(manifest, config_paths, require_program_match)
    if (
        type(manifest.get("schema_version")) is not int
        or type(manifest.get("items")) is not int
        or manifest["items"] < 0
        or manifest.get("integrity") != "ok"
        or manifest.get("secrets_included") is not False
        or not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("sha256", "")))
    ):
        raise ValueError("snapshot_manifest_invalid")
    expected_hash = manifest["sha256"]
    if _sha256(database) != expected_hash:
        raise ValueError("snapshot_hash_mismatch")
    counts, digests = {}, {}
    try:
        # immutable is safe only after rejecting WAL and validating the sealed file.
        uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != manifest["schema_version"]:
                raise ValueError("snapshot_schema_mismatch")
            if version not in {1, 2, 3, SCHEMA_VERSION}:
                raise ValueError("schema_incompatible")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("snapshot_integrity_failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("snapshot_foreign_key_violation")
            objects = connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
            ).fetchall()
            tables = [row[1] for row in objects if row[0] == "table"]
            if not {"items", "sources", "meta", "item_versions", "evidence", "receipts"} <= set(tables):
                raise ValueError("snapshot_required_tables_missing")
            if any("CREATE VIRTUAL TABLE" in (row[3] or "").upper() for row in objects):
                raise ValueError("snapshot_virtual_table_unsupported")
            for name in tables:
                counts[name], digests[name] = _table_profile(connection, name)
    except sqlite3.DatabaseError as exc:
        raise ValueError("snapshot_database_invalid") from exc
    if counts["items"] != manifest["items"]:
        raise ValueError("snapshot_item_count_mismatch")
    if manifest.get("manifest_version", 1) == 2:
        for name in ("events", "reports"):
            if counts.get(name, 0) != manifest[name]:
                raise ValueError(f"snapshot_{name}_count_mismatch")
    if _sha256(database) != expected_hash:
        raise ValueError("snapshot_changed_during_validation")
    schema_digest = hashlib.sha256(json.dumps(objects, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {
        "status": "valid",
        "schema_version": version,
        "migration_required": version != SCHEMA_VERSION,
        "sha256": expected_hash,
        "integrity": "ok",
        "foreign_key_violations": 0,
        "schema_digest": schema_digest,
        "table_counts": counts,
        "table_digests": digests,
        "manifest": manifest,
        "program_compatibility": program_compatibility,
        "config_verification": config_verification,
    }


def restore_snapshot(snapshot_dir, new_data_dir, *, config_paths=None, require_program_match=False):
    """Restore only into a new directory, with a fail-closed guard written first.

    On interruption the destination remains marked restoring; it is never silently
    reused or overwritten. Promotion is a separate explicit ownership operation.
    """
    snapshot_dir, destination = Path(snapshot_dir), Path(new_data_dir)
    validated = validate_snapshot(
        snapshot_dir, config_paths=config_paths, require_program_match=require_program_match
    )
    destination.mkdir(exist_ok=False)
    state = {
        "guard_version": 1,
        "state": "restoring",
        "mode": "shadow",
        "writer_enabled": False,
        "sender_enabled": False,
        "ownership": "unclaimed",
        "created_at": utcnow(),
        "source_sha256": validated["sha256"],
    }
    atomic_write_json(destination / GUARD_NAME, state, overwrite=False)
    with (
        (snapshot_dir / "radar.sqlite3").open("rb") as source,
        (destination / "radar.sqlite3").open("xb") as output,
    ):
        shutil.copyfileobj(source, output, length=1_048_576)
        output.flush()
        os.fsync(output.fileno())
    atomic_write_json(destination / "manifest.json", validated["manifest"], overwrite=False)
    restored = validate_snapshot(
        destination, config_paths=config_paths, require_program_match=require_program_match
    )
    if any(
        restored[key] != validated[key]
        for key in ("sha256", "schema_digest", "table_counts", "table_digests")
    ):
        raise ValueError("restore_reconciliation_failed")
    state.update(state="ready", validation=restored, completed_at=utcnow())
    atomic_write_json(destination / GUARD_NAME, state)
    return state


def assert_runtime_permission(data_dir, action):
    """Block collect/send after restore; allow ready shadow reads and analysis.

    Here writer means external collection or formal owner activity, not local
    shadow analysis. No guard means this is not a restored directory; callers
    must still apply their ordinary ownership and sending policy.
    """
    if action not in {"collect", "send", "read", "analyze"}:
        raise ValueError("unknown_runtime_action")
    guard = Path(data_dir) / GUARD_NAME
    if not guard.exists() and not guard.is_symlink():
        return {"mode": "not_restored"}
    state = _read_json(guard, "restore_state_invalid")
    if (
        type(state.get("guard_version")) is not int
        or state["guard_version"] != 1
        or state.get("state") != "ready"
        or state.get("mode") != "shadow"
        or state.get("writer_enabled") is not False
        or state.get("sender_enabled") is not False
        or state.get("ownership") != "unclaimed"
        or not re.fullmatch(r"[a-f0-9]{64}", str(state.get("source_sha256", "")))
        or not isinstance(state.get("validation"), dict)
        or state.get("validation", {}).get("status") != "valid"
    ):
        raise ValueError("restore_state_invalid")
    if action in {"collect", "send"}:
        raise ValueError("restore_shadow_only")
    return state
