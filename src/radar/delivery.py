"""Outbox and feedback. A lost send acknowledgement is an unresolved outcome, not retry permission."""

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .models import json_text, stable_id, utcnow
from .recovery import assert_runtime_permission

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
 delivery_key TEXT PRIMARY KEY, target_id TEXT NOT NULL, revision INTEGER NOT NULL,
 channel TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, attempted_at TEXT, receipt TEXT,
 error TEXT, owner_epoch TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
 feedback_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, label TEXT NOT NULL,
 actor TEXT NOT NULL, actor_type TEXT NOT NULL, interaction_receipt TEXT,
 created_at TEXT NOT NULL, UNIQUE(target_id,actor,actor_type)
);
"""


def initialize(db):
    db.conn.executescript(SCHEMA)


def get(db, key):
    row = db.conn.execute("SELECT * FROM outbox WHERE delivery_key=?", (key,)).fetchone()
    if not row:
        raise ValueError("delivery_not_found")
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    result["receipt"] = json.loads(result["receipt"]) if result["receipt"] else None
    return result


def enqueue(db, target_id, revision, channel, payload, owner_epoch=None):
    if (
        not target_id
        or not isinstance(revision, int)
        or revision < 1
        or channel not in {"test", "preview", "email", "private-web"}
    ):
        raise ValueError("invalid_delivery_target")
    serialized = json_text(payload)
    if len(serialized.encode()) > 200000:
        raise ValueError("delivery_too_large")
    key = stable_id(target_id, str(revision), channel)
    digest = stable_id(serialized)
    old = db.conn.execute("SELECT payload_hash FROM outbox WHERE delivery_key=?", (key,)).fetchone()
    if old:
        if old[0] != digest:
            raise ValueError("delivery_key_conflict")
        return get(db, key)
    with db.conn:
        db.conn.execute(
            "INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                target_id,
                revision,
                channel,
                serialized,
                digest,
                "pending",
                utcnow(),
                None,
                None,
                None,
                owner_epoch,
            ),
        )
    return get(db, key)


def send(db, key, transport, owner_epoch=None):
    row = get(db, key)
    if row["status"] == "unknown":
        return row
    if row["owner_epoch"] and row["owner_epoch"] != owner_epoch:
        raise ValueError("owner_epoch_mismatch")
    if row["status"] == "sent":
        if row["channel"] not in {"test", "preview"}:
            from .editor import record_publication

            with db.conn:
                record_publication(db, row["target_id"], row["revision"], delivery_key=key)
        return row
    if row["channel"] not in {"test", "preview"}:
        assert_runtime_permission(db.data_dir, "send")
        from .review import assert_publishable

        assert_publishable(db, row["target_id"], row["revision"], row["payload"])
    if row["status"] == "sending":
        # Process died between external effect and local receipt. No automatic second attempt.
        with db.conn:
            db.conn.execute(
                "UPDATE outbox SET status='unknown',error='interrupted_send' WHERE delivery_key=?", (key,)
            )
        return get(db, key)
    runtime = db.conn.execute(
        "SELECT value FROM meta WHERE key IN ('runtime_mode','operating_mode') AND value='shadow'"
    ).fetchone()
    if runtime and row["channel"] not in {"test", "preview"}:
        raise ValueError("shadow_delivery_disabled")
    with db.conn:
        changed = db.conn.execute(
            "UPDATE outbox SET status='sending',attempted_at=? WHERE delivery_key=? AND status='pending'",
            (utcnow(), key),
        ).rowcount
    if not changed:
        return get(db, key)
    try:
        # Preview/test is intrinsically local. An accidentally supplied network
        # transport is never invoked, including in a restored shadow directory.
        receipt = (
            {"message_id": "local-" + key, "local_only": True}
            if row["channel"] in {"test", "preview"}
            else transport(row["payload"], key)
        )
        if not isinstance(receipt, dict) or not receipt.get("message_id"):
            raise ValueError("missing_provider_receipt")
        with db.conn:
            db.conn.execute(
                "UPDATE outbox SET status='sent',receipt=?,error=NULL WHERE delivery_key=?",
                (json_text(receipt), key),
            )
            if row["channel"] not in {"test", "preview"}:
                from .editor import record_publication

                record_publication(db, row["target_id"], row["revision"], delivery_key=key)
    except Exception as exc:  # noqa: BLE001 - Any transport exception has an uncertain external outcome.
        # Do not store exception strings: transports can include credentials or recipients in them.
        with db.conn:
            db.conn.execute(
                "UPDATE outbox SET status='unknown',error=? WHERE delivery_key=?", (type(exc).__name__, key)
            )
    return get(db, key)


def add_feedback(db, target_id, label, actor, actor_type="operator", interaction_receipt=None):
    if label not in {"useful", "known", "too_broad", "duplicate", "follow"} or actor_type not in {
        "user",
        "operator",
        "reviewer",
    }:
        raise ValueError("invalid_feedback")
    if actor_type == "user" and not interaction_receipt:
        raise ValueError("user_feedback_requires_interaction_receipt")
    exists = db.conn.execute("SELECT 1 FROM events WHERE event_id=?", (target_id,)).fetchone()
    reports_exists = db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reports'"
    ).fetchone()
    if not exists and reports_exists:
        exists = db.conn.execute("SELECT 1 FROM reports WHERE report_id=?", (target_id,)).fetchone()
    # Fixture targets use an existing outbox; a random target cannot produce a user metric.
    if not exists:
        exists = db.conn.execute("SELECT 1 FROM outbox WHERE target_id=?", (target_id,)).fetchone()
    if not exists:
        raise ValueError("feedback_target_not_found")
    fid = stable_id("feedback", target_id, actor, actor_type)
    old = db.conn.execute("SELECT * FROM feedback WHERE feedback_id=?", (fid,)).fetchone()
    if old and old["label"] == label:
        return dict(old)
    with db.conn:
        db.conn.execute(
            """INSERT INTO feedback VALUES (?,?,?,?,?,?,?) ON CONFLICT(feedback_id)
            DO UPDATE SET label=excluded.label,interaction_receipt=excluded.interaction_receipt,created_at=excluded.created_at""",
            (fid, target_id, label, actor, actor_type, interaction_receipt, utcnow()),
        )
    return dict(db.conn.execute("SELECT * FROM feedback WHERE feedback_id=?", (fid,)).fetchone())


def eligible_alert(facts):
    if facts.get("kind") not in {
        "critical_regression",
        "security_impact",
        "correction",
        "blocking_work_improvement",
    }:
        return {"eligible": False, "reason": "digest_candidate_only"}
    for field in ("relevant", "new_evidence", "verified", "user_affected", "review_passed"):
        if facts.get(field) is not True:
            return {"eligible": False, "reason": f"requires_{field}"}
    if not isinstance(facts.get("waiting_cost"), str) or len(facts["waiting_cost"].strip()) < 5:
        return {"eligible": False, "reason": "missing_concrete_waiting_cost"}
    if facts["kind"] in {"critical_regression", "security_impact"}:
        if facts.get("version_matches") is not True:
            return {"eligible": False, "reason": "version_not_confirmed"}
        if facts.get("reproducible") is not True and facts.get("maintainer_confirmed") is not True:
            return {"eligible": False, "reason": "impact_unverified"}
    elif facts["kind"] == "blocking_work_improvement":
        if facts.get("work_currently_blocked") is not True or facts.get("reproducible") is not True:
            return {"eligible": False, "reason": "improvement_not_actionable"}
    elif facts.get("previous_action_invalidated") is not True:
        return {"eligible": False, "reason": "correction_not_actionable"}
    origins = facts.get("independent_origins", 0)
    if not isinstance(origins, int) or origins < 1:
        return {"eligible": False, "reason": "missing_original_evidence"}
    return {"eligible": True, "reason": "verified_direct_impact", "more_verification_needed": origins < 2}


def attention_allowed(now=None, sent_today=0, last_sent_at=None, correction=False):
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone_required")
    local = now.astimezone(ZoneInfo("Asia/Taipei"))
    if local.hour >= 23 or local.hour < 8:
        return {"allowed": False, "reason": "quiet_hours", "local_date": str(local.date())}
    if sent_today >= 2 and not correction:
        return {"allowed": False, "reason": "daily_cap"}
    if (
        last_sent_at
        and not correction
        and (now - datetime.fromisoformat(last_sent_at)).total_seconds() < 43200
    ):
        return {"allowed": False, "reason": "cooldown"}
    return {"allowed": True, "reason": "within_attention_budget"}
