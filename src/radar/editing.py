"""Auditable operator revisions of immutable, evidence-bound assessments.

This is an operator attestation of a reviewed edit, not independent review approval.
No model, network, tool or receipt-file access occurs here. The operator verifies the
review receipt and supplies the complete wire ASSESSMENT_SCHEMA, without derived fields.
"""

import hashlib
import json

from . import editor
from .models import json_text, normalized_time, stable_id, utcnow


def apply_operator_revision(db, *, source_assessment_id, value, operator, reason, review_receipt):
    """Append one checked variant; never update the source assessment or frozen reports.

    Identity, prompt version, cutoff and generation call are inherited from the source.
    A full SHA256 binds the submitted value and parent value. Same parent/content returns
    the existing variant with its original audit metadata, even on an interrupted rerun.
    A new report/packet must independently pass review before publication. This function
    owns its transaction and rejects an ambient transaction rather than committing it.
    """
    if not all(
        isinstance(field, str) and field.strip() and len(field) <= 4096
        for field in (operator, reason, review_receipt)
    ):
        raise ValueError("operator_revision_provenance_required")
    if not isinstance(source_assessment_id, str) or not source_assessment_id.strip():
        raise ValueError("operator_revision_source_required")
    if db.conn.in_transaction:
        raise ValueError("operator_revision_transaction_active")
    editor.initialize(db)
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        source = db.conn.execute(
            "SELECT * FROM assessments WHERE assessment_id=?", (source_assessment_id,)
        ).fetchone()
        if source is None:
            raise ValueError("operator_revision_source_missing")
        if source["status"] not in {"validated", "degraded"} or not source["call_id"]:
            raise ValueError("operator_revision_source_invalid")
        event_row = db.conn.execute(
            "SELECT * FROM event_versions WHERE event_id=? AND revision=?",
            (source["event_id"], source["event_revision"]),
        ).fetchone()
        if event_row is None:
            raise ValueError("operator_revision_event_missing")
        cutoff = normalized_time(source["cutoff"])
        if normalized_time(event_row["observed_at"]) > cutoff:
            raise ValueError("operator_event_after_cutoff")
        event = json.loads(event_row["snapshot"])
        if (
            event.get("event_id") != source["event_id"]
            or event.get("revision") != source["event_revision"]
            or stable_id(json_text(event["members"])) != source["evidence_fingerprint"]
        ):
            raise ValueError("operator_revision_event_mismatch")
        checked = editor.validate_assessment(db, event, value, cutoff)
        value_hash = hashlib.sha256(json_text(value).encode()).hexdigest()
        source_hash = hashlib.sha256(json_text(json.loads(source["result"])).encode()).hexdigest()
        assessment_id = stable_id("operator-revision-v1", source_assessment_id, source_hash, value_hash)
        existing = db.conn.execute(
            "SELECT result FROM assessments WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        if existing:
            return {
                "assessment_id": assessment_id,
                "changed": False,
                "provider_calls": 0,
                "result": json.loads(existing[0]),
                "independent_review": "pending",
            }
        checked["operator_revision"] = {
            "schema_version": 1,
            "source_assessment_id": source_assessment_id,
            "source_value_sha256": source_hash,
            "value_sha256": value_hash,
            "source_model": source["model"],
            "source_status": source["status"],
            "source_call_id": source["call_id"],
            "operator": operator,
            "reason": reason,
            "review_receipt": review_receipt,
            "new_provider_call": False,
            "independent_review": "pending",
        }
        variant = source["model"] + ":operator-revision:" + assessment_id[:12]
        db.conn.execute(
            "INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                assessment_id,
                source["event_id"],
                source["event_revision"],
                source["prompt_version"],
                variant,
                source["call_id"],
                source["evidence_fingerprint"],
                source["cutoff"],
                utcnow(),
                "validated",
                json_text(checked),
            ),
        )
        return {
            "assessment_id": assessment_id,
            "changed": True,
            "provider_calls": 0,
            "result": checked,
            "independent_review": "pending",
        }
