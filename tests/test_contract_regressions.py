"""Offline review/receipt contract regressions; all model/transport inputs are fixtures."""

import copy
import json
from pathlib import Path

import pytest
from test_review import attest, make_report

from radar import cli, delivery, review
from radar.store import Store


@pytest.mark.parametrize("field", ["document", "source_versions", "generation_call_ids", "revision"])
def test_import_rejects_mutated_packet_with_retained_hash(tmp_path, field):
    with Store(tmp_path) as db:
        report = make_report(db)
        packet = copy.deepcopy(review.report_packet(db, report["report_id"]))
        if field == "document":
            packet[field]["cards"][0]["claims"][0]["text"] = "Different claim reviewed."
        elif field == "source_versions":
            next(iter(packet[field].values()))["text"] = "Different evidence reviewed."
        elif field == "generation_call_ids":
            packet[field] = []
        else:
            packet[field] += 1
        with pytest.raises(ValueError, match="review_packet_changed"):
            attest(db, packet, "editorial")


def test_import_cannot_remove_generator_to_bypass_independence(tmp_path):
    with Store(tmp_path) as db:
        report = make_report(db)
        packet = review.report_packet(db, report["report_id"])
        generator = packet["generation_call_ids"][0]
        packet["generation_call_ids"] = []
        with pytest.raises(ValueError, match="review_packet_changed|review_not_independent"):
            attest(db, packet, "editorial", session=generator)


@pytest.mark.parametrize("change", ["source_text", "source_hash", "source_context", "outbox_payload"])
def test_sender_rechecks_source_packet_and_queued_content(tmp_path, change):
    with Store(tmp_path) as db:
        report = make_report(db)
        packet = review.report_packet(db, report["report_id"])
        attest(db, packet, "source_compare")
        attest(db, packet, "editorial")
        delivery.initialize(db)
        queued = delivery.enqueue(db, report["report_id"], 1, "email", report)
        version = next(iter(packet["source_versions"].values()))
        pair = version["item_id"], version["revision"]
        if change == "source_text":
            db.conn.execute("UPDATE item_versions SET text='changed' WHERE item_id=? AND revision=?", pair)
        elif change == "source_hash":
            db.conn.execute(
                "UPDATE item_versions SET content_hash='changed' WHERE item_id=? AND revision=?", pair
            )
        elif change == "source_context":
            db.conn.execute(
                "UPDATE item_context_versions SET context=? WHERE item_id=? AND revision=?",
                (json.dumps(version["context"] | {"url": "https://example.com/changed"}), *pair),
            )
        else:
            report["markdown"] = "Different payload than independently reviewed."
            db.conn.execute(
                "UPDATE outbox SET payload=? WHERE delivery_key=?",
                (json.dumps(report), queued["delivery_key"]),
            )
        db.conn.commit()
        invoked = []
        with pytest.raises(ValueError, match="independent_review_pending|delivery_content_not_reviewed"):
            delivery.send(db, queued["delivery_key"], lambda *args: invoked.append(args))
        assert invoked == []


def fake_collection(monkeypatch, tmp_path):
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"schema_version": 1, "sources": []}), encoding="utf-8")
    calls = []

    def collect(*args):
        calls.append(True)
        return {"status": "success", "data": {"changed": False, "model_calls": 0}, "errors": []}

    monkeypatch.setattr(cli, "collect", collect)
    return [
        "--data-dir",
        str(tmp_path / "data"),
        "--config",
        str(config),
        "--idempotency-key",
        "fixture-collection",
        "collect",
    ], calls


def test_idempotent_cli_returns_same_complete_receipt(tmp_path, monkeypatch, capsys):
    args, calls = fake_collection(monkeypatch, tmp_path)
    assert cli.main(args) == 0
    original = json.loads(capsys.readouterr().out)
    assert "resources" in original and original["artifacts"]
    assert cli.main(args) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert calls == [True]
    assert replayed == original


def test_receipt_artifact_matches_delivered_receipt(tmp_path, monkeypatch, capsys):
    args, _ = fake_collection(monkeypatch, tmp_path)
    assert cli.main(args) == 0
    delivered = json.loads(capsys.readouterr().out)
    assert len(delivered["artifacts"]) == 1
    persisted = json.loads(Path(delivered["artifacts"][0]).read_text(encoding="utf-8"))
    assert persisted == delivered
