"""Backup provenance and compatibility checks use isolated nonsecret fixtures."""

import hashlib
import json

import pytest

from radar import __version__, editor, recovery, store
from radar.events import rebuild
from radar.models import Item, Scan
from radar.store import Store


def snapshot(tmp_path, with_config=True):
    config = tmp_path / "source-config.json"
    config.write_text('{"sources": [], "fixture_marker": "contents-not-exported"}', encoding="utf-8")
    configs = {"sources": config} if with_config else None
    with Store(tmp_path / "source") as db:
        item = Item("s", "p", "https://example.com/p", "AI model", "test text")
        db.save_scan(Scan("s", [item], complete=True), "one")
        rebuild(db)
        editor.initialize(db)
        db.conn.execute(
            "INSERT INTO reports VALUES (?,?,?,?,?,?,?,?)",
            (
                "fixture-report",
                "fixture-key",
                "2026-10-22T00:00:00Z",
                "2026-10-21T00:00:00Z",
                "2026-10-22T00:00:00Z",
                1,
                "fixture",
                "{}",
            ),
        )
        db.conn.commit()
        manifest = db.backup(tmp_path / "snapshot", config_paths=configs)
    return tmp_path / "snapshot", config, manifest


def write_manifest(path, manifest):
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_manifest_records_code_config_and_snapshot_counts_without_config_contents(tmp_path):
    folder, config, manifest = snapshot(tmp_path)
    assert manifest["manifest_version"] == 2
    assert manifest["program"]["version"] == __version__
    assert len(manifest["program"]["source_sha256"]) == 64
    assert manifest["config_hashes"] == {"sources": hashlib.sha256(config.read_bytes()).hexdigest()}
    assert manifest["config_status"] == "captured"
    assert manifest["events"] == manifest["reports"] == manifest["items"] == 1
    assert "contents-not-exported" not in (folder / "manifest.json").read_text(encoding="utf-8")
    checked = recovery.validate_snapshot(folder, config_paths={"sources": config}, require_program_match=True)
    assert checked["program_compatibility"] == "exact"
    assert checked["config_verification"] == "matched"
    state = recovery.restore_snapshot(
        folder, tmp_path / "restored", config_paths={"sources": config}, require_program_match=True
    )
    assert state["mode"] == "shadow" and not state["sender_enabled"]


@pytest.mark.parametrize("field", ["events", "reports"])
def test_snapshot_counts_are_verified_against_copied_database(tmp_path, field):
    folder, _, manifest = snapshot(tmp_path)
    manifest[field] += 1
    write_manifest(folder, manifest)
    with pytest.raises(ValueError, match=f"snapshot_{field}_count_mismatch"):
        recovery.validate_snapshot(folder)


def test_changed_configuration_is_rejected_before_restore_target_creation(tmp_path):
    folder, config, _ = snapshot(tmp_path)
    config.write_text('{"sources": ["different"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot_config_mismatch"):
        recovery.restore_snapshot(folder, tmp_path / "rejected", config_paths={"sources": config})
    assert not (tmp_path / "rejected").exists()


def test_program_difference_is_explicit_and_strict_restore_can_reject_it(tmp_path):
    folder, _, manifest = snapshot(tmp_path)
    manifest["program"]["version"] = "0.0.0"
    write_manifest(folder, manifest)
    assert recovery.validate_snapshot(folder)["program_compatibility"] == "different"
    with pytest.raises(ValueError, match="snapshot_program_mismatch"):
        recovery.restore_snapshot(folder, tmp_path / "rejected", require_program_match=True)
    assert not (tmp_path / "rejected").exists()


def test_legacy_manifest_remains_valid_but_cannot_claim_version_or_config_match(tmp_path):
    folder, config, manifest = snapshot(tmp_path)
    for key in ("manifest_version", "program", "config_hashes", "config_status", "events", "reports"):
        manifest.pop(key)
    write_manifest(folder, manifest)
    checked = recovery.validate_snapshot(folder)
    assert checked["program_compatibility"] == "unknown_legacy"
    assert checked["config_verification"] == "unknown_legacy"
    assert recovery.restore_snapshot(folder, tmp_path / "restored")["mode"] == "shadow"
    with pytest.raises(ValueError, match="snapshot_provenance_missing"):
        recovery.validate_snapshot(folder, config_paths={"sources": config})
    with pytest.raises(ValueError, match="snapshot_provenance_missing"):
        recovery.validate_snapshot(folder, require_program_match=True)


def test_omitted_configs_are_marked_unverified_and_unknown_manifest_is_rejected(tmp_path):
    folder, _, manifest = snapshot(tmp_path, with_config=False)
    assert manifest["config_status"] == "not_supplied" and manifest["config_hashes"] == {}
    assert recovery.validate_snapshot(folder)["config_verification"] == "not_supplied"
    manifest["manifest_version"] = 999
    write_manifest(folder, manifest)
    with pytest.raises(ValueError, match="snapshot_manifest_version_unsupported"):
        recovery.validate_snapshot(folder)


def test_invalid_config_input_does_not_leave_partial_backup(tmp_path):
    with Store(tmp_path / "source") as db, pytest.raises(ValueError, match="backup_config_invalid"):
        db.backup(tmp_path / "rejected", config_paths={"../escape": tmp_path / "missing.json"})
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("field,value", [("version", 1), ("source_sha256", int("1" * 64))])
def test_malformed_program_metadata_is_not_treated_as_compatible(tmp_path, field, value):
    folder, _, manifest = snapshot(tmp_path)
    manifest["program"][field] = value
    write_manifest(folder, manifest)
    with pytest.raises(ValueError, match="snapshot_manifest_invalid"):
        recovery.validate_snapshot(folder)


def test_code_changed_during_backup_leaves_no_sealed_manifest(tmp_path, monkeypatch):
    identity = store.program_identity()
    identities = iter((identity, identity | {"version": "changed"}))
    monkeypatch.setattr(store, "program_identity", lambda: next(identities))
    with Store(tmp_path / "source") as db, pytest.raises(ValueError, match="backup_provenance_changed"):
        db.backup(tmp_path / "unsealed")
    assert (tmp_path / "unsealed/radar.sqlite3").is_file()
    assert not (tmp_path / "unsealed/manifest.json").exists()
    with pytest.raises(ValueError, match="snapshot_manifest_invalid"):
        recovery.validate_snapshot(tmp_path / "unsealed")
