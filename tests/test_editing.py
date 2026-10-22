import copy
import json

import pytest
from test_review import attest, make_report

from radar import editor, review
from radar.store import Store

SINCE = "2026-10-21T00:00:00Z"
CUTOFF = "2026-10-22T00:00:00Z"


def apply(db, source_id, value, **changes):
    from radar.editing import apply_operator_revision

    return apply_operator_revision(
        db,
        source_assessment_id=source_id,
        value=value,
        **(
            {
                "operator": "fixture-operator",
                "reason": "Clarify author attribution",
                "review_receipt": "validation/fixture-editorial-review.json",
            }
            | changes
        ),
    )


def draft(report):
    card = report["cards"][0]
    value = {key: copy.deepcopy(card[key]) for key in editor.ASSESSMENT_SCHEMA["required"]}
    for section in ("claims", "reactions"):
        for claim in value[section]:
            claim["evidence"] = [
                {key: ref[key] for key in editor.REFERENCE_SCHEMA["required"]} for ref in claim["evidence"]
            ]
    return value


def test_operator_revision_is_immutable_no_model_call_and_requires_new_review(tmp_path):
    with Store(tmp_path) as db:
        report = make_report(db)
        original_id = report["cards"][0]["assessment_id"]
        before = dict(
            db.conn.execute("SELECT * FROM assessments WHERE assessment_id=?", (original_id,)).fetchone()
        )
        original_document = db.conn.execute(
            "SELECT document FROM reports WHERE report_id=?", (report["report_id"],)
        ).fetchone()[0]
        packet = review.report_packet(db, report["report_id"])
        attest(db, packet, "source_compare")
        attest(db, packet, "editorial")
        original_calls = db.conn.execute("SELECT count(*) FROM model_calls").fetchone()[0]
        value = draft(report)
        value["claims"][0]["text"] = "作者表示使用 8 GB RAM，實測 v2.1。"
        revised = apply(db, original_id, value)
        assert revised["changed"] and revised["provider_calls"] == 0
        assert not apply(db, original_id, value)["changed"]
        source_after = dict(
            db.conn.execute("SELECT * FROM assessments WHERE assessment_id=?", (original_id,)).fetchone()
        )
        assert source_after == before
        new_row = db.conn.execute(
            "SELECT * FROM assessments WHERE assessment_id=?", (revised["assessment_id"],)
        ).fetchone()
        assert new_row["call_id"] == before["call_id"] and new_row["cutoff"] == before["cutoff"]
        assert ":operator-revision:" in new_row["model"]
        assert new_row["prompt_version"] == before["prompt_version"]
        assert db.conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == original_calls
        current = editor.build_digest(db, SINCE, CUTOFF)["publication_payload"]
        assert current["report_id"] != report["report_id"]
        assert current["cards"][0]["assessment_id"] == revised["assessment_id"]
        assert current["cards"][0]["operator_revision"]["source_assessment_id"] == original_id
        assert current["cards"][0]["operator_revision"]["new_provider_call"] is False
        assert review.status(db, current["report_id"])["status"] == "pending"
        assert review.status(db, report["report_id"])["status"] == "pass"
        assert (
            db.conn.execute(
                "SELECT document FROM reports WHERE report_id=?", (report["report_id"],)
            ).fetchone()[0]
            == original_document
        )


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("quote", "quote_not_found"),
        ("event", "assessment_schema"),
        ("revision", "evidence_not_in_event"),
        ("hash", "evidence_version_mismatch"),
    ],
)
def test_operator_revision_rejects_invalid_quote_or_changed_identity(tmp_path, mutation, error):
    with Store(tmp_path) as db:
        report = make_report(db)
        value = draft(report)
        if mutation == "quote":
            value["claims"][0]["evidence"][0]["quote"] = "invented"
        elif mutation == "event":
            value["event_id"] = "other"
        elif mutation == "revision":
            value["claims"][0]["evidence"][0]["revision"] = 2
        else:
            value["claims"][0]["evidence"][0]["content_hash"] = "wrong"
        with pytest.raises(ValueError, match=error):
            apply(db, report["cards"][0]["assessment_id"], value)
        assert db.conn.execute("SELECT count(*) FROM assessments").fetchone()[0] == 1


@pytest.mark.parametrize("field", ["operator", "reason", "review_receipt"])
def test_operator_revision_requires_attributed_review(tmp_path, field):
    with Store(tmp_path) as db:
        report = make_report(db)
        with pytest.raises(ValueError, match="operator_revision_provenance_required"):
            apply(db, report["cards"][0]["assessment_id"], draft(report), **{field: " "})


def test_operator_revision_checks_original_cutoff_and_has_unique_content_variants(tmp_path):
    with Store(tmp_path) as db:
        report = make_report(db)
        original_id = report["cards"][0]["assessment_id"]
        value = draft(report)
        first = apply(db, original_id, value)
        different = copy.deepcopy(value)
        different["limitations"].append("no_independent_test")
        second = apply(db, original_id, different)
        assert first["assessment_id"] != second["assessment_id"]
        assert (
            json.loads(
                db.conn.execute(
                    "SELECT result FROM assessments WHERE assessment_id=?", (first["assessment_id"],)
                ).fetchone()[0]
            )["operator_revision"]["value_sha256"]
            != (second["result"]["operator_revision"]["value_sha256"])
        )
        db.conn.execute(
            "UPDATE assessments SET cutoff='2026-10-21T12:00:00Z' WHERE assessment_id=?", (original_id,)
        )
        db.conn.commit()
        with pytest.raises(ValueError, match="operator_event_after_cutoff"):
            apply(db, original_id, value)


def test_operator_revision_rejects_evidence_observed_after_original_cutoff(tmp_path):
    with Store(tmp_path) as db:
        report = make_report(db)
        value = draft(report)
        reference = value["claims"][0]["evidence"][0]
        db.conn.execute(
            "UPDATE item_versions SET observed_at='2026-10-22T01:00:00Z' WHERE item_id=? AND revision=?",
            (reference["item_id"], reference["revision"]),
        )
        db.conn.commit()
        with pytest.raises(ValueError, match="evidence_after_cutoff"):
            apply(db, report["cards"][0]["assessment_id"], value)
        assert db.conn.execute("SELECT count(*) FROM assessments").fetchone()[0] == 1
