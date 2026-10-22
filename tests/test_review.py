import json

import pytest
from test_editor import Provider, seed

from radar import delivery, review
from radar.editor import analyze, build_digest
from radar.llm import ModelRunner, quota_config
from radar.store import Store


def make_report(db):
    item, _ = seed(db)
    analyze(
        db,
        ModelRunner(db, Provider(item), quota_config(), "fixture-generator"),
        since="2026-10-21T00:00:00Z",
        cutoff="2026-10-22T00:00:00Z",
    )
    return build_digest(db, "2026-10-21T00:00:00Z", "2026-10-22T00:00:00Z")["publication_payload"]


def attest(db, packet, role, verdict="pass", session="fixture-independent"):
    return review.record(
        db,
        packet,
        role=role,
        verdict=verdict,
        model="fixture-editorial-model" if role == "editorial" else "fixture-source-model",
        session_id=session,
        tool_receipt="fixture-not-real-review",
        verified_by="test",
        details={"findings_checked": ["synthetic fixture only"]},
    )


def test_sender_needs_source_and_editorial_review_on_exact_content(tmp_path):
    with Store(tmp_path) as db:
        report = make_report(db)
        delivery.initialize(db)
        row = delivery.enqueue(db, report["report_id"], 1, "email", report)
        calls = []
        transport = lambda payload, key: calls.append(key) or {"message_id": "fixture-only"}
        with pytest.raises(ValueError, match="independent_review_pending"):
            delivery.send(db, row["delivery_key"], transport)
        packet = review.report_packet(db, report["report_id"])
        assert packet["source_versions"]
        attest(db, packet, "source_compare")
        assert review.status(db, report["report_id"])["status"] == "pending"
        attest(db, packet, "editorial")
        assert delivery.send(db, row["delivery_key"], transport)["status"] == "sent"
        assert delivery.send(db, row["delivery_key"], transport)["status"] == "sent"
        assert len(calls) == 1
        with pytest.raises(ValueError, match="delivery_content_not_reviewed"):
            review.assert_publishable(db, report["report_id"], 1, report | {"markdown": "altered"})


def test_review_is_invalidated_by_changed_document_and_new_failure(tmp_path):
    with Store(tmp_path) as db:
        report = make_report(db)
        packet = review.report_packet(db, report["report_id"])
        attest(db, packet, "source_compare")
        attest(db, packet, "editorial")
        attest(db, packet, "editorial", "fail")
        assert review.status(db, report["report_id"])["status"] == "pending"
        report["cards"][0]["title"] = "tampered"
        db.conn.execute(
            "UPDATE reports SET document=? WHERE report_id=?", (json.dumps(report), report["report_id"])
        )
        db.conn.commit()
        with pytest.raises(ValueError, match="review_packet_changed"):
            attest(db, packet, "editorial")
        assert review.status(db, report["report_id"])["roles"] == {}


def test_generator_cannot_sign_own_packet(tmp_path):
    with Store(tmp_path) as db:
        report = make_report(db)
        packet = review.report_packet(db, report["report_id"])
        with pytest.raises(ValueError, match="review_not_independent"):
            attest(db, packet, "editorial", session=packet["generation_call_ids"][0])
