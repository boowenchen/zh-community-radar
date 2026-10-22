from datetime import UTC, datetime

import pytest

from radar.delivery import add_feedback, attention_allowed, eligible_alert, enqueue, initialize, send
from radar.store import Store


def test_unknown_delivery_is_never_blindly_retried(tmp_path, monkeypatch):
    from radar import review

    # This unit isolates transport outcomes; test_review covers the real gate.
    monkeypatch.setattr(review, "assert_publishable", lambda *args: None)
    with Store(tmp_path) as db:
        initialize(db)
        row = enqueue(db, "event1", 1, "email", {"text": "verified content"})
        calls = []

        def transport(payload, key):
            calls.append(key)
            raise TimeoutError("lost acknowledgement")

        assert send(db, row["delivery_key"], transport)["status"] == "unknown"
        assert send(db, row["delivery_key"], transport)["status"] == "unknown"
        assert len(calls) == 1


def test_preview_cannot_invoke_transport_and_wrong_owner_cannot_write(tmp_path):
    with Store(tmp_path) as db:
        initialize(db)
        row = enqueue(db, "fixture", 1, "preview", {"text": "local"}, owner_epoch="vm-1")

        def forbidden(*args):
            raise AssertionError("network transport invoked")

        with pytest.raises(ValueError, match="owner_epoch_mismatch"):
            send(db, row["delivery_key"], forbidden, owner_epoch="pc-2")
        assert send(db, row["delivery_key"], forbidden, owner_epoch="vm-1")["receipt"]["local_only"]
        another = enqueue(db, "fixture", 2, "preview", {}, owner_epoch="vm-1")
        db.conn.execute("UPDATE outbox SET status='sending' WHERE delivery_key=?", (another["delivery_key"],))
        db.conn.commit()
        with pytest.raises(ValueError, match="owner_epoch_mismatch"):
            send(db, another["delivery_key"], forbidden, owner_epoch="pc-2")
        assert (
            db.conn.execute(
                "SELECT status FROM outbox WHERE delivery_key=?", (another["delivery_key"],)
            ).fetchone()[0]
            == "sending"
        )


def test_outbox_content_conflict_and_feedback_idempotency(tmp_path):
    with Store(tmp_path) as db:
        initialize(db)
        first = enqueue(db, "event1", 1, "test", {"text": "x"})
        assert enqueue(db, "event1", 1, "test", {"text": "x"}) == first
        with pytest.raises(ValueError, match="delivery_key_conflict"):
            enqueue(db, "event1", 1, "test", {"text": "y"})
        a = add_feedback(db, "event1", "useful", "test-user", "user", "test-receipt")
        b = add_feedback(db, "event1", "useful", "test-user", "user", "test-receipt")
        assert a == b and db.conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 1
        with pytest.raises(ValueError, match="user_feedback_requires_interaction_receipt"):
            add_feedback(db, "event1", "useful", "pretend-user", "user", None)


def test_hot_release_and_financial_rumor_are_not_alerts():
    base = {
        "relevant": True,
        "new_evidence": True,
        "verified": True,
        "waiting_cost": "work blocked",
        "user_affected": True,
        "independent_origins": 2,
        "review_passed": True,
    }
    assert not eligible_alert(base | {"kind": "release"})["eligible"]
    assert not eligible_alert(base | {"kind": "popularity"})["eligible"]
    assert not eligible_alert(base | {"kind": "financial_rumor"})["eligible"]
    assert eligible_alert(
        base | {"kind": "critical_regression", "version_matches": True, "reproducible": True}
    )["eligible"]
    assert not eligible_alert(base | {"kind": "critical_regression", "version_matches": False})["eligible"]
    assert not eligible_alert(base | {"kind": "blocking_work_improvement", "reproducible": True})["eligible"]
    assert eligible_alert(
        base | {"kind": "blocking_work_improvement", "reproducible": True, "work_currently_blocked": True}
    )["eligible"]


def test_quiet_hours_and_daily_cap_are_in_taipei():
    # 16 UTC = midnight Taipei; no default bypass even for severity critical.
    assert not attention_allowed(datetime(2026, 10, 21, 16, tzinfo=UTC), 0)["allowed"]
    assert attention_allowed(datetime(2026, 10, 22, 4, tzinfo=UTC), 1)["allowed"]
    assert not attention_allowed(datetime(2026, 10, 22, 4, tzinfo=UTC), 2)["allowed"]
