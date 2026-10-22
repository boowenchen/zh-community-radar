import pytest

from radar.models import Item, Scan
from radar.store import Store


def item(text="evidence", observed="2026-10-22T01:00:00Z"):
    return Item(
        source_id="test",
        native_id="one",
        url="https://example.com/one",
        title="Test",
        text=text,
        published_at=None,
        fetched_at=observed,
    )


def test_content_versions_and_first_seen_survive_repeated_collection(tmp_path):
    with Store(tmp_path) as db:
        assert db.save_scan(Scan("test", [item()], complete=True), "r1")["added"] == 1
        assert db.save_scan(Scan("test", [item()], complete=True), "r2")["changed"] == 0
        assert (
            db.save_scan(Scan("test", [item("edited", "2026-10-22T02:00:00Z")], complete=True), "r3")[
                "updated"
            ]
            == 1
        )
        row = db.items()[0]
        assert row["first_seen_at"] == "2026-10-22T01:00:00Z"
        assert row["published_at"] is None
        assert row["revision"] == 2
        assert db.conn.execute("select count(*) from item_versions").fetchone()[0] == 2


def test_current_failure_not_hidden_by_previous_success(tmp_path):
    with Store(tmp_path) as db:
        db.save_scan(Scan("test", [item()], complete=True), "r1")
        db.save_scan(Scan("test", status="blocked", error="http_403"), "r2")
        state = db.source_status()[0]
        assert state["status"] == "blocked"
        assert state["last_success_at"]
        assert state["error"] == "http_403"


def test_partial_scan_does_not_advance_complete_checkpoint(tmp_path):
    with Store(tmp_path) as db:
        db.save_scan(Scan("test", [item()], complete=True, checkpoint={"cursor": 1}), "r1")
        db.save_scan(Scan("test", [item("new")], status="partial", checkpoint={"cursor": 99}), "r2")
        assert db.checkpoint("test") == {"cursor": 1}
        assert db.items()[0]["text"] == "new"


def test_invalid_item_rolls_back_whole_scan_and_checkpoint(tmp_path):
    with Store(tmp_path) as db:
        bad = item()
        bad.source_id = "different"
        with pytest.raises(ValueError):
            db.save_scan(Scan("test", [item(), bad], complete=True, checkpoint={"cursor": 5}), "r1")
        assert db.items() == []
        assert db.checkpoint("test") == {}


def test_receipt_key_cannot_replay_different_request(tmp_path):
    with Store(tmp_path) as db:
        db.save_receipt("key", "request-a", {"status": "success"})
        assert db.receipt("key", "request-a")["status"] == "success"
        with pytest.raises(ValueError, match="idempotency_conflict"):
            db.receipt("key", "request-b")


def test_evidence_offset_must_match_original_version(tmp_path):
    with Store(tmp_path) as db:
        db.save_scan(Scan("test", [item("model is useful but expensive")], complete=True), "r1")
        row = db.items()[0]
        ref = db.add_evidence(row["item_id"], 1, "useful but expensive")
        assert ref["quote"] == "useful but expensive"
        with pytest.raises(ValueError, match="quote_not_found"):
            db.add_evidence(row["item_id"], 1, "model is free")


def test_backup_is_consistent_and_does_not_overwrite_existing(tmp_path):
    with Store(tmp_path / "data") as db:
        db.save_scan(Scan("test", [item()], complete=True), "r1")
        snapshot = db.backup(tmp_path / "snapshot")
        assert snapshot["integrity"] == "ok"
        with pytest.raises(FileExistsError):
            db.backup(tmp_path / "snapshot")
