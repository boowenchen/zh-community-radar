import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from radar import recovery
from radar.models import Item, Scan
from radar.store import SCHEMA_VERSION, Store


def make_snapshot(tmp_path):
    with Store(tmp_path / "original") as db:
        item = Item("test", "one", "https://example.com/one", "Release", "Version 1 improves speed")
        db.save_scan(Scan("test", [item], complete=True, checkpoint={"cursor": "one"}), "first")
        db.add_evidence(item.item_id, 1, "improves speed")
        db.save_receipt("already-sent", "request", {"delivery": "acknowledged"})
        db.conn.execute('CREATE TABLE "future ledger" (id TEXT PRIMARY KEY, payload BLOB)')
        db.conn.execute('INSERT INTO "future ledger" VALUES (?,?)', ("stable-id", b"opaque"))
        db.conn.commit()
        db.backup(tmp_path / "snapshot")
    return tmp_path / "snapshot"


def reseal(snapshot):
    path = snapshot / "manifest.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["sha256"] = hashlib.sha256((snapshot / "radar.sqlite3").read_bytes()).hexdigest()
    path.write_text(json.dumps(doc), encoding="utf-8")
    return doc


def test_restore_preserves_all_tables_ids_evidence_and_checkpoint_in_shadow(tmp_path):
    snapshot = make_snapshot(tmp_path)
    expected = recovery.validate_snapshot(snapshot)
    result = recovery.restore_snapshot(snapshot, tmp_path / "restored")
    assert result["state"] == "ready"
    assert result["mode"] == "shadow"
    assert result["writer_enabled"] is False
    assert result["sender_enabled"] is False
    assert result["ownership"] == "unclaimed"
    assert result["validation"]["table_digests"] == expected["table_digests"]
    assert result["validation"]["table_counts"]["future ledger"] == 1
    assert (tmp_path / "restored" / "radar.sqlite3").read_bytes() == (snapshot / "radar.sqlite3").read_bytes()
    with closing(sqlite3.connect(tmp_path / "restored" / "radar.sqlite3")) as db:
        assert json.loads(db.execute("SELECT checkpoint FROM sources").fetchone()[0]) == {"cursor": "one"}
        assert db.execute("SELECT idempotency_key FROM receipts").fetchone()[0] == "already-sent"


def test_restore_refuses_existing_directory_without_overwrite(tmp_path):
    snapshot = make_snapshot(tmp_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    keep = destination / "keep.txt"
    keep.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(FileExistsError):
        recovery.restore_snapshot(snapshot, destination)
    assert keep.read_text(encoding="utf-8") == "do not overwrite"
    assert not (destination / "radar.sqlite3").exists()


def test_changed_database_is_rejected_before_creating_restore_target(tmp_path):
    snapshot = make_snapshot(tmp_path)
    with (snapshot / "radar.sqlite3").open("ab") as stream:
        stream.write(b"modified")
    with pytest.raises(ValueError, match="snapshot_hash_mismatch"):
        recovery.restore_snapshot(snapshot, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_corrupt_database_rejected_even_if_hash_matches_manifest(tmp_path):
    snapshot = make_snapshot(tmp_path)
    (snapshot / "radar.sqlite3").write_bytes(b"not a database")
    reseal(snapshot)
    with pytest.raises(ValueError, match="snapshot_database_invalid"):
        recovery.validate_snapshot(snapshot)


@pytest.mark.parametrize("version", [0, 100_000])
def test_unknown_database_schema_rejected(tmp_path, version):
    snapshot = make_snapshot(tmp_path)
    with closing(sqlite3.connect(snapshot / "radar.sqlite3")) as db:
        db.execute(f"PRAGMA user_version={version}")
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    manifest = reseal(snapshot)
    manifest["schema_version"] = version
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_incompatible"):
        recovery.validate_snapshot(snapshot)


def test_foreign_key_corruption_rejected_even_when_integrity_is_ok(tmp_path):
    snapshot = make_snapshot(tmp_path)
    with closing(sqlite3.connect(snapshot / "radar.sqlite3")) as db:
        db.execute("CREATE TABLE child (id TEXT REFERENCES items(item_id))")
        db.execute("INSERT INTO child VALUES ('missing-parent')")
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    reseal(snapshot)
    with pytest.raises(ValueError, match="snapshot_foreign_key_violation"):
        recovery.validate_snapshot(snapshot)


def test_manifest_item_count_is_verified(tmp_path):
    snapshot = make_snapshot(tmp_path)
    manifest = reseal(snapshot)
    manifest["items"] += 1
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot_item_count_mismatch"):
        recovery.validate_snapshot(snapshot)


def test_snapshot_wal_is_not_silently_ignored(tmp_path):
    snapshot = make_snapshot(tmp_path)
    (snapshot / "radar.sqlite3-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(ValueError, match="snapshot_has_wal"):
        recovery.validate_snapshot(snapshot)


def test_manifest_schema_must_match_database(tmp_path):
    snapshot = make_snapshot(tmp_path)
    manifest = reseal(snapshot)
    manifest["schema_version"] = 1 if SCHEMA_VERSION != 1 else 2
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot_schema_mismatch"):
        recovery.validate_snapshot(snapshot)


def test_restored_data_cannot_collect_or_send_but_can_analyze(tmp_path):
    snapshot = make_snapshot(tmp_path)
    target = tmp_path / "restore"
    recovery.restore_snapshot(snapshot, target)
    for action in ("collect", "send"):
        with pytest.raises(ValueError, match="restore_shadow_only"):
            recovery.assert_runtime_permission(target, action)
    for action in ("read", "analyze"):
        assert recovery.assert_runtime_permission(target, action)["mode"] == "shadow"


@pytest.mark.parametrize("contents", ["not json", '{"state":"restoring"}', '{"state":"ready","mode":"live"}'])
def test_corrupt_incomplete_or_promoted_without_proof_guard_fails_closed(tmp_path, contents):
    (tmp_path / "restore-state.json").write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="restore_state_invalid"):
        recovery.assert_runtime_permission(tmp_path, "collect")


def test_new_non_restored_data_has_no_restore_specific_restriction(tmp_path):
    assert recovery.assert_runtime_permission(tmp_path, "collect")["mode"] == "not_restored"
    with pytest.raises(ValueError, match="unknown_runtime_action"):
        recovery.assert_runtime_permission(tmp_path, "invented-action")


def test_interrupted_restore_keeps_guard_and_never_enables_sender(tmp_path, monkeypatch):
    snapshot = make_snapshot(tmp_path)
    target = tmp_path / "interrupted"

    def interrupt_copy(source, output, **kwargs):
        output.write(source.read(10))
        raise OSError("simulated interruption")

    monkeypatch.setattr(recovery.shutil, "copyfileobj", interrupt_copy)
    with pytest.raises(OSError, match="simulated"):
        recovery.restore_snapshot(snapshot, target)
    state = json.loads((target / "restore-state.json").read_text(encoding="utf-8"))
    assert state["state"] == "restoring"
    assert state["sender_enabled"] is False
    with pytest.raises(ValueError, match="restore_state_invalid"):
        recovery.assert_runtime_permission(target, "read")


def test_invalid_validation_payload_fails_with_stable_error(tmp_path):
    snapshot = make_snapshot(tmp_path)
    target = tmp_path / "restored"
    state = recovery.restore_snapshot(snapshot, target)
    state["validation"] = None
    (target / "restore-state.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="restore_state_invalid"):
        recovery.assert_runtime_permission(target, "send")


def test_failed_atomic_replace_preserves_previous_report(tmp_path, monkeypatch):
    target = tmp_path / "daily.html"
    target.write_text("previous complete report", encoding="utf-8")

    def fail_replace(*args):
        raise OSError("simulated write failure")

    monkeypatch.setattr(recovery.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        recovery.atomic_write_text(target, "new report")
    assert target.read_text(encoding="utf-8") == "previous complete report"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_create_does_not_overwrite_existing_file(tmp_path):
    target = tmp_path / "receipt.json"
    recovery.atomic_write_text(target, "first", overwrite=False)
    with pytest.raises(FileExistsError):
        recovery.atomic_write_text(target, "second", overwrite=False)
    assert target.read_text(encoding="utf-8") == "first"
