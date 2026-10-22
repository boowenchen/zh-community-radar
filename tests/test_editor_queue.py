"""Bounded editor queue and publication contracts, using local fixtures only."""

import json

import pytest
from test_editor import Provider, assessment, model_assessment, seed

from radar import delivery, editor, events, review
from radar.llm import ModelError, ModelRunner, quota_config
from radar.models import Item, Scan, json_text, stable_id, utcnow
from radar.store import Store

SINCE = "2026-10-21T00:00:00Z"
CUTOFF = "2026-10-22T00:00:00Z"


def many(db, count):
    items = [
        Item(
            "test",
            str(n),
            f"https://example.com/{n}",
            "AI inference v2.1",
            seed.__defaults__[0],
            published_at="2026-10-21T12:00:00Z",
            fetched_at="2026-10-21T13:00:00Z",
        )
        for n in range(count)
    ]
    db.save_scan(Scan("test", items=items, complete=True, observed_at="2026-10-21T13:00:00Z"), "fixture")
    events.rebuild(db)
    return {item.item_id: item for item in items}


def runner(db, provider):
    config = quota_config()
    for limit in config["limits"].values():
        limit.update(calls=100, input_tokens=1000000, output_tokens=1000000)
    return ModelRunner(db, provider, config, "fixture-only")


def test_invalid_first_batch_is_parked_and_next_batch_progresses(tmp_path):
    with Store(tmp_path) as db:
        items = many(db, 5)
        target = events.list_events(db, cutoff=CUTOFF, limit=10)[-1]["members"][0]["item_id"]

        class MixedProvider:
            name, model = "fixture", "fixture-editor"

            def __init__(self):
                self.seen = []

            def generate(self, system, payload, schema, max_output_tokens):
                event = next(
                    e for e in events.list_events(db, cutoff=CUTOFF) if e["event_id"] == payload["event_id"]
                )
                item_id = event["members"][0]["item_id"]
                self.seen.append(item_id)
                value = model_assessment(items[item_id], payload)
                if item_id != target:
                    value["claims"][0]["evidence"][0]["evidence_id"] = "absent-unit"
                return {
                    "content": value,
                    "usage": {"input_tokens": 1, "output_tokens": 1, "basis": "observed"},
                }

        provider = MixedProvider()
        model = runner(db, provider)
        first = editor.analyze(db, model, SINCE, CUTOFF, limit=4)
        assert first["pending_events"] == 1 and not first["analysis_complete"]
        assert len(first["dispositions"]) == 4
        assert all(d["status"] == "parked_invalid" for d in first["dispositions"])
        second = editor.analyze(db, model, SINCE, CUTOFF, limit=4)
        assert second["analyzed"] == 1 and second["analysis_complete"] and not second["model_required"]
        assert provider.seen.count(target) == 1
        assert len(provider.seen) == 9  # Four invalid outputs + four bounded repairs + one valid output.
        assert editor.analyze(db, model, SINCE, CUTOFF)["model_calls"] == 0


def test_pending_count_and_progress_beyond_first_thousand(tmp_path):
    with Store(tmp_path) as db:
        items = many(db, 1001)
        editor.initialize(db)
        top = events.list_events(db, cutoff=CUTOFF, limit=1000)
        with db.conn:
            for event in top:
                value = assessment(items[event["members"][0]["item_id"]])
                db.conn.execute(
                    "INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        stable_id("cached", event["event_id"]),
                        event["event_id"],
                        event["revision"],
                        editor.PROMPT_VERSION,
                        "fixture-editor",
                        "fixture-call",
                        "fixture-fingerprint",
                        CUTOFF,
                        utcnow(),
                        "validated",
                        json_text(value),
                    ),
                )
        cached_items = {e["members"][0]["item_id"] for e in top}
        remaining = next(item for key, item in items.items() if key not in cached_items)
        provider = Provider(remaining)
        result = editor.analyze(db, runner(db, provider), SINCE, CUTOFF)
        assert result["scanned_events"] == 1001 and result["cached"] == 1000
        assert result["analyzed"] == provider.calls == 1
        assert result["analysis_complete"] and not result["model_required"]


def test_digest_can_reach_valid_card_beyond_forty_pending_events(tmp_path):
    with Store(tmp_path) as db:
        items = many(db, 41)
        target = events.list_events(db, cutoff=CUTOFF, limit=100)[-1]
        item = items[target["members"][0]["item_id"]]
        editor.analyze(db, runner(db, Provider(item)), SINCE, CUTOFF, event_ids=[target["event_id"]])
        report = editor.build_digest(db, SINCE, CUTOFF)
        assert [card["event_id"] for card in report["cards"]] == [target["event_id"]]
        assert report["pending_analysis"] == 40


@pytest.mark.parametrize("decision", ["watch", "omit"])
def test_digest_does_not_call_completed_editor_decision_pending(tmp_path, decision):
    with Store(tmp_path) as db:
        item, _ = seed(db)
        before = editor.build_digest(db, SINCE, CUTOFF)
        assert before["pending_analysis"] == 1

        class Decided(Provider):
            def generate(self, *args):
                value = super().generate(*args)
                value["content"]["decision"] = decision
                return value

        assert editor.analyze(db, runner(db, Decided(item)), SINCE, CUTOFF)["analysis_complete"]
        report = editor.build_digest(db, SINCE, CUTOFF)
        assert report["pending_analysis"] == 0
        assert report["excluded"][0]["reason"] == "assessment_" + decision
        assert report["report_id"] != before["report_id"]


def test_digest_distinguishes_parked_models_from_deferred_models(tmp_path):
    with Store(tmp_path) as db:
        seed(db)

        class Unavailable:
            name, model = "fixture", "unknown"
            error = "model_call_outcome_unknown"

            def generate(self, *args):
                raise ModelError(self.error)

        unknown = Unavailable()
        editor.analyze(db, runner(db, unknown), SINCE, CUTOFF)
        parked = editor.build_digest(db, SINCE, CUTOFF)
        assert parked["pending_analysis"] == 0 and parked["excluded"][0]["reason"] == "analysis_parked"
        assert parked["analysis_dispositions"][0]["status"] == "parked_unknown"
        transient = Unavailable()
        transient.model, transient.error = "transient", "model_timeout"
        editor.analyze(db, runner(db, transient), SINCE, CUTOFF)
        deferred = editor.build_digest(db, SINCE, CUTOFF)
        assert deferred["pending_analysis"] == 1 and len(deferred["analysis_dispositions"]) == 2


def test_transient_errors_back_off_and_require_explicit_resume_after_two_attempts(tmp_path, monkeypatch):
    with Store(tmp_path) as db:
        item, event = seed(db)

        class Unavailable:
            name, model = "fixture", "unavailable"
            recovered = False

            def generate(self, *args):
                if self.recovered:
                    return {
                        "content": model_assessment(item, args[1]),
                        "usage": {"input_tokens": 1, "output_tokens": 1, "basis": "observed"},
                    }
                raise ModelError("model_timeout")

        model = runner(db, Unavailable())
        monkeypatch.setattr(editor, "utcnow", lambda: "2026-10-22T00:00:00Z")
        first = editor.analyze(db, model, SINCE, CUTOFF)
        assert first["dispositions"][0]["status"] == "deferred_transient"
        assert first["pending_events"] == 1
        again = editor.analyze(db, model, SINCE, CUTOFF)
        assert again["model_calls"] == 0 and not again["analysis_complete"]
        monkeypatch.setattr(editor, "utcnow", lambda: "2026-10-22T00:16:00Z")
        second = editor.analyze(db, model, SINCE, CUTOFF)
        assert second["dispositions"][0]["status"] == "parked_transient"
        assert second["analysis_complete"] and second["model_calls"] == 1
        editor.retry_parked(
            db,
            event["event_id"],
            event["revision"],
            "unavailable",
            reason="Fixture provider recovery verified",
        )
        model.provider.recovered = True
        recovered = editor.analyze(db, model, SINCE, CUTOFF)
        assert recovered["model_calls"] == recovered["analyzed"] == 1
        assert recovered["analysis_complete"] and recovered["dispositions"] == []
        stored = db.conn.execute("SELECT history FROM analysis_dispositions").fetchone()[0]
        assert "Fixture provider recovery verified" in stored


def test_unknown_model_outcome_is_parked_and_cannot_use_transient_resume(tmp_path):
    with Store(tmp_path) as db:
        _item, event = seed(db)

        class Unknown:
            name, model = "fixture", "unknown"

            def generate(self, *args):
                raise ModelError("model_call_outcome_unknown")

        model = runner(db, Unknown())
        first = editor.analyze(db, model, SINCE, CUTOFF)
        assert first["dispositions"][0]["status"] == "parked_unknown"
        assert first["analysis_complete"]
        assert editor.analyze(db, model, SINCE, CUTOFF)["model_calls"] == 0
        with pytest.raises(ValueError, match="only_parked_transient_can_resume"):
            editor.retry_parked(
                db, event["event_id"], event["revision"], "unknown", reason="Cannot infer outcome"
            )


def test_budget_exhaustion_is_not_a_permanent_disposition(tmp_path):
    with Store(tmp_path) as db:
        item, _ = seed(db)
        config = quota_config()
        config["limits"]["run"]["calls"] = 0
        result = editor.analyze(db, ModelRunner(db, Provider(item), config, "fixture"), SINCE, CUTOFF)
        assert result["pending_events"] == 1 and not result["analysis_complete"]
        assert result["dispositions"] == [] and result["model_calls"] == 0


def test_publication_payload_is_canonical_and_preview_does_not_consume_event(tmp_path, monkeypatch):
    with Store(tmp_path) as db:
        item, _ = seed(db)
        editor.analyze(db, runner(db, Provider(item)), SINCE, CUTOFF)
        report = editor.build_digest(db, SINCE, CUTOFF, artifact_dir=tmp_path / "reports")
        payload = report["publication_payload"]
        assert report["artifacts"] and not {"artifacts", "changed", "notify", "publication_payload"} & set(
            payload
        )
        assert payload == json.loads(
            db.conn.execute(
                "SELECT document FROM reports WHERE report_id=?", (report["report_id"],)
            ).fetchone()[0]
        )
        later = editor.build_digest(db, SINCE, "2026-10-22T00:30:00Z")
        assert len(later["cards"]) == 1
        delivery.initialize(db)
        packet = review.report_packet(db, report["report_id"])
        for role in ("source_compare", "editorial"):
            review.record(
                db,
                packet,
                role=role,
                verdict="pass",
                model="fixture-editorial-model",
                session_id="fixture-independent",
                tool_receipt="fixture-only",
                verified_by="test",
                details={"findings_checked": ["fixture only"]},
            )
        queued = delivery.enqueue(db, report["report_id"], 1, "email", payload)
        received = []
        monkeypatch.setattr(editor, "utcnow", lambda: "2026-10-22T00:45:00Z")
        assert (
            delivery.send(
                db,
                queued["delivery_key"],
                lambda content, key: received.append(content) or {"message_id": "fixture-only"},
            )["status"]
            == "sent"
        )
        assert received == [payload]
        editor.record_publication(
            db,
            report["report_id"],
            1,
            delivery_key=queued["delivery_key"],
            published_at="2026-10-22T00:45:00Z",
        )
        # Actual publication after this cutoff must not rewrite past knowledge.
        assert len(editor.build_digest(db, SINCE, "2026-10-22T00:40:00Z")["cards"]) == 1
        assert editor.build_digest(db, SINCE, "2026-10-22T01:00:00Z")["cards"] == []


def test_local_preview_success_cannot_be_recorded_as_publication(tmp_path):
    with Store(tmp_path) as db:
        item, _ = seed(db)
        editor.analyze(db, runner(db, Provider(item)), SINCE, CUTOFF)
        report = editor.build_digest(db, SINCE, CUTOFF)
        delivery.initialize(db)
        row = delivery.enqueue(db, report["report_id"], 1, "preview", report["publication_payload"])
        delivery.send(db, row["delivery_key"], None)
        with pytest.raises(ValueError, match="publication_requires_confirmed_delivery"):
            editor.record_publication(db, report["report_id"], 1, delivery_key=row["delivery_key"])


def test_publication_journal_does_not_commit_its_callers_transaction(tmp_path):
    with Store(tmp_path) as db:
        item, _ = seed(db)
        editor.analyze(db, runner(db, Provider(item)), SINCE, CUTOFF)
        report = editor.build_digest(db, SINCE, CUTOFF)
        delivery.initialize(db)
        queued = delivery.enqueue(db, report["report_id"], 1, "email", report["publication_payload"])
        db.conn.execute("DROP TABLE report_publications")
        db.conn.commit()
        with pytest.raises(RuntimeError, match="rollback fixture"), db.conn:
            db.conn.execute(
                "UPDATE outbox SET status='sent',receipt=? WHERE delivery_key=?",
                (json_text({"message_id": "fixture"}), queued["delivery_key"]),
            )
            editor.record_publication(db, report["report_id"], 1, delivery_key=queued["delivery_key"])
            raise RuntimeError("rollback fixture")
        assert delivery.get(db, queued["delivery_key"])["status"] == "pending"
        assert not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='report_publications'").fetchone()
