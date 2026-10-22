"""Journal an operator's manual email delivery; this module cannot send messages.

Only the human-authorized driver may call the external connector. A connector
receipt and operator attestation are trusted inputs, not cryptographic proof.
Mutators own their transaction and reject an already active caller transaction.
The normal shadow/restore sender guards are neither removed nor bypassed.
"""

import hashlib
import html
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from . import delivery, editor, review
from .models import json_text, normalized_time, stable_id, utcnow

SCHEMA = """CREATE TABLE IF NOT EXISTS manual_deliveries (
 manual_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, revision INTEGER NOT NULL,
 delivery_key TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, recipient TEXT NOT NULL,
 subject TEXT NOT NULL, html_body TEXT NOT NULL, html_sha256 TEXT NOT NULL,
 envelope_sha256 TEXT NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
 packet_hash TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
 attempted_at TEXT, completed_at TEXT, receipt TEXT, history TEXT NOT NULL,
 UNIQUE(target_id,revision)
)"""
RECEIPT_CLOCK_SKEW_SECONDS = 120
MARKDOWN_HTTP_LINK = re.compile(r"(?<!!)\[[^\[\]\r\n]+\]\((https?://[^\s<>\"'\\()]*)\)", re.IGNORECASE)


def sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_link(url):
    """Accept a narrow public Web-link syntax, without normalizing its destination."""
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and parsed.port != 0
            and not any(ord(char) < 33 or ord(char) == 127 for char in url)
            and not any(char in parsed.netloc for char in "%\\")
        )
    except ValueError:
        return False


def _escaped_markdown_links(markdown):
    """Keep all visible Markdown verbatim; only give safe links click targets."""
    parts, previous = [], 0
    for match in MARKDOWN_HTTP_LINK.finditer(markdown):
        parts.append(html.escape(markdown[previous : match.start()], quote=True))
        visible = html.escape(match[0], quote=True)
        if _safe_link(match[1]):
            destination = html.escape(match[1], quote=True)
            parts.append(f'<a href="{destination}" rel="noopener noreferrer">{visible}</a>')
        else:
            parts.append(visible)
        previous = match.end()
    parts.append(html.escape(markdown[previous:], quote=True))
    return "".join(parts)


def render_html(payload):
    """An exact, escaped presentation of reviewed Markdown; no extra claims or assets."""
    if not isinstance(payload, dict) or not isinstance(payload.get("markdown"), str):
        raise ValueError("manual_canonical_markdown_required")  # noqa: TRY004 - Stable operator error.
    return (
        '<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><body>'
        '<pre style="white-space:pre-wrap;font:16px/1.7 sans-serif">'
        + _escaped_markdown_links(payload["markdown"])
        + "</pre></body></html>"
    )


def _label(value, name, minimum=1, maximum=500):
    if (
        not isinstance(value, str)
        or not minimum <= len(value.strip()) <= maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("manual_" + name + "_invalid")
    return value


def _recipient(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}",
            value,
        )
        or len(value) > 254
    ):
        raise ValueError("manual_single_recipient_required")
    return value


def _initialize(db):
    if db.conn.in_transaction:
        raise ValueError("manual_requires_own_transaction")
    # Existing initialize helpers use executescript; only call before our transaction.
    delivery.initialize(db)
    db.conn.execute(SCHEMA)


def get(db, manual_id):
    row = db.conn.execute("SELECT * FROM manual_deliveries WHERE manual_id=?", (manual_id,)).fetchone()
    if not row:
        raise ValueError("manual_delivery_not_found")
    result = dict(row)
    for key in ("payload", "receipt", "history"):
        result[key] = json.loads(result[key]) if result[key] else None
    result["manual_dispatch_allowed"] = False
    return result


def _owner(row, owner):
    if not owner or row["owner"] != owner:
        raise ValueError("manual_owner_mismatch")


def _binding(row):
    payload = row["payload"]
    body = render_html(payload)
    if (
        sha256(json_text(payload)) != row["payload_sha256"]
        or body != row["html_body"]
        or sha256(body) != row["html_sha256"]
        or sha256(json_text([row["recipient"], row["subject"], body])) != row["envelope_sha256"]
    ):
        raise ValueError("manual_content_binding_changed")


def _checked_packet(db, row):
    """Read-only recheck inside BEGIN IMMEDIATE; never call review.initialize here."""
    _binding(row)
    packet = review.report_packet(db, row["target_id"])
    if (
        packet["packet_hash"] != row["packet_hash"]
        or packet["revision"] != row["revision"]
        or json_text(packet["document"]) != json_text(row["payload"])
    ):
        raise ValueError("manual_review_packet_changed")
    latest = {}
    for record in db.conn.execute(
        "SELECT role,verdict FROM reviews WHERE target_id=? AND revision=? AND packet_hash=? ORDER BY rowid DESC",
        (row["target_id"], row["revision"], row["packet_hash"]),
    ):
        latest.setdefault(record["role"], record["verdict"])
    if not packet["document"]["cards"] or any(
        latest.get(role) != "pass" for role in ("source_compare", "editorial")
    ):
        raise ValueError("independent_review_pending")


def _history(row, action, **extra):
    return json_text(row["history"] + [{"at": utcnow(), "action": action, "owner": row["owner"], **extra}])


def _checked_outbox(db, row, status):
    outgoing = delivery.get(db, row["delivery_key"])
    if (
        outgoing["owner_epoch"] != row["owner"]
        or outgoing["status"] != status
        or outgoing["target_id"] != row["target_id"]
        or outgoing["revision"] != row["revision"]
        or outgoing["channel"] != "email"
        or json_text(outgoing["payload"]) != json_text(row["payload"])
        or outgoing["payload_hash"] != stable_id(json_text(row["payload"]))
    ):
        raise ValueError("manual_outbox_changed")


def manual_prepare(db, report_id, revision, payload, *, recipient, subject, owner):
    """Bind one fixed recipient/content pair; preparation never grants dispatch."""
    _initialize(db)
    _recipient(recipient)
    _label(subject, "subject", maximum=200)
    _label(owner, "owner", maximum=100)
    if type(revision) is not int or revision < 1:
        raise ValueError("manual_revision_invalid")
    review.assert_publishable(db, report_id, revision, payload)
    packet = review.report_packet(db, report_id)
    body = render_html(payload)
    if len(body.encode()) > 200_000:
        raise ValueError("manual_body_too_large")
    manual_id = stable_id(report_id, str(revision), "manual-email")
    key = stable_id(report_id, str(revision), "email")
    binding = sha256(json_text([recipient, subject, body]))
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        old = db.conn.execute(
            "SELECT manual_id FROM manual_deliveries WHERE manual_id=?", (manual_id,)
        ).fetchone()
        if old:
            row = get(db, manual_id)
            _owner(row, owner)
            if row["envelope_sha256"] != binding or row["payload_sha256"] != sha256(json_text(payload)):
                raise ValueError("manual_preparation_conflict")
            _checked_packet(db, row)
            return row
        if db.conn.execute("SELECT 1 FROM outbox WHERE delivery_key=?", (key,)).fetchone():
            raise ValueError("manual_existing_outbox_requires_reconciliation")
        now = utcnow()
        db.conn.execute(
            "INSERT INTO manual_deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                manual_id,
                report_id,
                revision,
                key,
                owner,
                recipient,
                subject,
                body,
                sha256(body),
                binding,
                json_text(payload),
                sha256(json_text(payload)),
                packet["packet_hash"],
                "prepared",
                now,
                None,
                None,
                None,
                json_text([{"at": now, "action": "prepared", "owner": owner}]),
            ),
        )
        row = get(db, manual_id)
        _checked_packet(db, row)
        db.conn.execute(
            "INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                report_id,
                revision,
                "email",
                json_text(payload),
                stable_id(json_text(payload)),
                "manual_prepared",
                now,
                None,
                None,
                "manual_only_no_automatic_dispatch",
                owner,
            ),
        )
    return get(db, manual_id)


def manual_begin(db, manual_id, *, owner):
    """Only prepared→attempted grants one driver action; reentry never grants resend."""
    _initialize(db)
    row = get(db, manual_id)
    _owner(row, owner)
    if row["status"] == "prepared":
        review.assert_publishable(db, row["target_id"], row["revision"], row["payload"])
    allowed = False
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        row = get(db, manual_id)
        _owner(row, owner)
        if row["status"] == "prepared":
            _checked_packet(db, row)
            _checked_outbox(db, row, "manual_prepared")
            now = utcnow()
            db.conn.execute(
                "UPDATE manual_deliveries SET status='attempted',attempted_at=?,history=? WHERE manual_id=?",
                (now, _history(row, "attempted"), manual_id),
            )
            db.conn.execute(
                "UPDATE outbox SET status='unknown',attempted_at=?,error='manual_outcome_pending' WHERE delivery_key=?",
                (now, row["delivery_key"]),
            )
            allowed = True
        elif row["status"] == "attempted":
            _checked_outbox(db, row, "unknown")
            db.conn.execute(
                "UPDATE manual_deliveries SET status='unknown',history=? WHERE manual_id=?",
                (_history(row, "reentry_outcome_unknown"), manual_id),
            )
    return get(db, manual_id) | {"manual_dispatch_allowed": allowed}


def manual_unknown(db, manual_id, *, owner):
    _initialize(db)
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        row = get(db, manual_id)
        _owner(row, owner)
        if row["status"] not in {"attempted", "unknown"}:
            raise ValueError("manual_unknown_requires_attempt")
        _checked_outbox(db, row, "unknown")
        if row["status"] == "attempted":
            db.conn.execute(
                "UPDATE manual_deliveries SET status='unknown',history=? WHERE manual_id=?",
                (_history(row, "connector_outcome_unknown"), manual_id),
            )
    return get(db, manual_id)


def manual_complete(
    db,
    manual_id,
    *,
    owner,
    connector_receipt,
    actual_recipient,
    actual_subject,
    actual_html_sha256,
    operator_attestation,
    reconcile=False,
):
    """Import a driver-verified receipt. Unknown outcomes need explicit receipt reconciliation.

    connector_receipt must come from the real email connector result, not model
    output: provider=email_connector, message_id, sent_at, tool_receipt. The
    driver's attestation names the evidence it actually checked. No network occurs.
    """
    _initialize(db)
    row = get(db, manual_id)
    _owner(row, owner)
    _label(operator_attestation, "operator_attestation", minimum=20, maximum=1000)
    if not isinstance(connector_receipt, dict) or set(connector_receipt) != {
        "provider",
        "message_id",
        "sent_at",
        "tool_receipt",
    }:
        raise ValueError("manual_connector_receipt_invalid")
    if connector_receipt["provider"] != "email_connector":
        raise ValueError("manual_connector_receipt_invalid")
    _label(connector_receipt["message_id"], "message_id", maximum=256)
    _label(connector_receipt["tool_receipt"], "tool_receipt", maximum=500)
    sent_at = normalized_time(connector_receipt["sent_at"])
    if datetime.fromisoformat(sent_at) > datetime.fromisoformat(utcnow()) + timedelta(
        seconds=RECEIPT_CLOCK_SKEW_SECONDS
    ):
        raise ValueError("manual_receipt_future_time")
    if (
        actual_recipient != row["recipient"]
        or actual_subject != row["subject"]
        or actual_html_sha256 != row["html_sha256"]
    ):
        raise ValueError("manual_actual_message_mismatch")
    receipt = connector_receipt | {
        "normalized_sent_at": sent_at,
        "actual_recipient": actual_recipient,
        "actual_subject": actual_subject,
        "actual_html_sha256": actual_html_sha256,
        "operator": owner,
        "operator_attestation": operator_attestation,
        "envelope_sha256": row["envelope_sha256"],
        "manual_id": manual_id,
    }
    if row["status"] == "sent":
        _binding(row)
        _checked_outbox(db, row, "sent")
        previous = {
            k: v
            for k, v in row["receipt"].items()
            if k not in {"publication_effective_at", "publication_clock_skew_seconds"}
        }
        if json_text(previous) != json_text(receipt):
            raise ValueError("manual_receipt_conflict")
        return row
    if row["status"] not in {"attempted", "unknown"}:
        raise ValueError("manual_completion_requires_attempt")
    if row["status"] == "unknown" and reconcile is not True:
        raise ValueError("manual_unknown_requires_explicit_receipt_reconciliation")
    if sent_at < row["attempted_at"]:
        raise ValueError("manual_receipt_predates_attempt")
    review.assert_publishable(db, row["target_id"], row["revision"], row["payload"])
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        row = get(db, manual_id)
        _owner(row, owner)
        _checked_packet(db, row)
        if row["status"] not in {"attempted", "unknown"} or (
            row["status"] == "unknown" and reconcile is not True
        ):
            raise ValueError("manual_state_changed")
        _checked_outbox(db, row, "unknown")
        recorded_at = normalized_time(utcnow())
        effective_at = min(sent_at, recorded_at)
        receipt.update(
            publication_effective_at=effective_at,
            publication_clock_skew_seconds=max(
                0,
                int((datetime.fromisoformat(sent_at) - datetime.fromisoformat(recorded_at)).total_seconds()),
            ),
        )
        db.conn.execute(
            "UPDATE outbox SET status='sent',receipt=?,error=NULL WHERE delivery_key=?",
            (json_text(receipt), row["delivery_key"]),
        )
        editor.record_publication(
            db, row["target_id"], row["revision"], delivery_key=row["delivery_key"], published_at=effective_at
        )
        db.conn.execute(
            "UPDATE manual_deliveries SET status='sent',completed_at=?,receipt=?,history=? WHERE manual_id=?",
            (
                utcnow(),
                json_text(receipt),
                _history(row, "receipt_reconciled" if reconcile else "receipt_imported"),
                manual_id,
            ),
        )
    return get(db, manual_id)
