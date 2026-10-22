"""Manual receipt contracts using fake connectors only; no external sends."""

import json
from html.parser import HTMLParser

import pytest
from test_review import attest, make_report

from radar import delivery, editor, review
from radar import manual_delivery as manual
from radar.models import utcnow
from radar.recovery import assert_runtime_permission, validate_snapshot
from radar.store import Store

OWNER = "fixture-operator"
RECIPIENT = "fixture@example.com"


class HtmlInspection(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self.tags = [], [], []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs))

    def handle_data(self, data):
        self.text.append(data)


def test_manual_html_safe_source_links_preserve_all_visible_reviewed_text():
    markdown = (
        '# 原文 <script>alert("no")</script> &amp;\n'
        "- [原文](https://example.com/post?a=1&b=%22#note)\n"
        "- [來源 <tag>](http://news.ycombinator.com/item?id=123)\n"
        "![無圖片載入](https://example.com/image.png)\n"
        "[不支援巢狀網址](https://example.com/path(with-parens))\n"
    )
    payload = {"markdown": markdown, "cards": []}
    body = manual.render_html(payload)
    parsed = HtmlInspection(body)
    assert "".join(parsed.text) == markdown
    assert [link["href"] for link in parsed.links] == [
        "https://example.com/post?a=1&b=%22#note",
        "http://news.ycombinator.com/item?id=123",
    ]
    assert all(link["rel"] == "noopener noreferrer" for link in parsed.links)
    assert set(parsed.tags) == {"html", "meta", "body", "pre", "a"}
    assert payload["markdown"] == markdown
    assert manual.render_html(payload) == body


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,unsafe",
        "//example.com/relative",
        "https:///missing-host",
        "https://user:password@example.com/",
        "https://example.com:99999/",
        "https://example.com:0/",
        "https://example.com%2f.evil.test/",
        "https://example.com\\@evil.test/",
        "https://example.com/\x00tail",
        "https://example.com/\x7ftail",
        "https://example.com/\npath",
        'https://example.com/"onclick=unsafe',
    ],
)
def test_manual_html_unsafe_links_remain_escaped_text_only(url):
    markdown = "[原文](" + url + ")"
    body = manual.render_html({"markdown": markdown})
    parsed = HtmlInspection(body)
    assert not parsed.links
    assert "".join(parsed.text) == markdown
    assert set(parsed.tags) == {"html", "meta", "body", "pre"}


def prepare(db):
    report = make_report(db)
    packet = review.report_packet(db, report["report_id"])
    for role in ("source_compare", "editorial"):
        attest(db, packet, role)
    return manual.manual_prepare(
        db,
        report["report_id"],
        report["report_revision"],
        report,
        recipient=RECIPIENT,
        subject="Fixture daily report",
        owner=OWNER,
    )


def completion(row, **overrides):
    return {
        "owner": OWNER,
        "connector_receipt": {
            "provider": "email_connector",
            "message_id": "fixture-message",
            "sent_at": utcnow(),
            "tool_receipt": "fixture-only-no-external-send",
        },
        "actual_recipient": row["recipient"],
        "actual_subject": row["subject"],
        "actual_html_sha256": row["html_sha256"],
        "operator_attestation": "Fixture driver verified synthetic connector result and exact content.",
        **overrides,
    }


def test_manual_prepare_begin_complete_is_bound_and_idempotent(tmp_path):
    with Store(tmp_path) as db:
        row = prepare(db)
        assert row["status"] == "prepared" and not row["manual_dispatch_allowed"]
        assert row["html_sha256"] == manual.sha256(manual.render_html(row["payload"]))
        assert row["html_body"].count("<!doctype html>") == 1
        again = manual.manual_prepare(
            db,
            row["target_id"],
            row["revision"],
            row["payload"],
            recipient=RECIPIENT,
            subject=row["subject"],
            owner=OWNER,
        )
        assert again["manual_id"] == row["manual_id"]
        begun = manual.manual_begin(db, row["manual_id"], owner=OWNER)
        assert begun["manual_dispatch_allowed"] and begun["status"] == "attempted"
        args = completion(row)
        result = manual.manual_complete(db, row["manual_id"], **args)
        assert result["status"] == "sent" and not result["manual_dispatch_allowed"]
        assert manual.manual_complete(db, row["manual_id"], **args) == result
        assert db.conn.execute("SELECT count(*) FROM report_publications").fetchone()[0] == 1
        assert delivery.get(db, row["delivery_key"])["status"] == "sent"
        db.backup(tmp_path / "snapshot")
        assert validate_snapshot(tmp_path / "snapshot")["table_counts"]["manual_deliveries"] == 1


@pytest.mark.parametrize("recipient", ["a@example.com,b@example.com", "a@example.com\nBcc:x@example.com"])
def test_only_one_fixed_recipient_can_be_prepared(tmp_path, recipient):
    with Store(tmp_path) as db:
        row = prepare(db)
        with pytest.raises(ValueError, match="manual_single_recipient_required"):
            manual.manual_prepare(
                db,
                row["target_id"],
                row["revision"],
                row["payload"],
                recipient=recipient,
                subject=row["subject"],
                owner=OWNER,
            )
        with pytest.raises(ValueError, match="manual_preparation_conflict"):
            manual.manual_prepare(
                db,
                row["target_id"],
                row["revision"],
                row["payload"],
                recipient="other@example.com",
                subject=row["subject"],
                owner=OWNER,
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("actual_recipient", "wrong@example.com"),
        ("actual_subject", "changed"),
        ("actual_html_sha256", "0" * 64),
    ],
)
def test_receipt_requires_exact_recipient_subject_and_body(tmp_path, field, value):
    with Store(tmp_path) as db:
        row = prepare(db)
        manual.manual_begin(db, row["manual_id"], owner=OWNER)
        with pytest.raises(ValueError, match="manual_actual_message_mismatch"):
            manual.manual_complete(db, row["manual_id"], **completion(row, **{field: value}))
        assert delivery.get(db, row["delivery_key"])["status"] == "unknown"
        assert not db.conn.execute("SELECT * FROM report_publications").fetchone()


def test_lost_acknowledgement_never_grants_resend_and_requires_reconcile(tmp_path):
    with Store(tmp_path) as db:
        row = prepare(db)
        assert manual.manual_begin(db, row["manual_id"], owner=OWNER)["manual_dispatch_allowed"]
        again = manual.manual_begin(db, row["manual_id"], owner=OWNER)
        assert again["status"] == "unknown" and not again["manual_dispatch_allowed"]
        assert not manual.manual_begin(db, row["manual_id"], owner=OWNER)["manual_dispatch_allowed"]
        with pytest.raises(ValueError, match="manual_unknown_requires_explicit_receipt_reconciliation"):
            manual.manual_complete(db, row["manual_id"], **completion(row))
        assert (
            manual.manual_complete(db, row["manual_id"], **completion(row, reconcile=True))["status"]
            == "sent"
        )


@pytest.mark.parametrize("stage", ["begin", "complete"])
def test_new_failed_review_blocks_begin_or_receipt_import(tmp_path, stage):
    with Store(tmp_path) as db:
        row = prepare(db)
        if stage == "complete":
            manual.manual_begin(db, row["manual_id"], owner=OWNER)
        attest(db, review.report_packet(db, row["target_id"]), "editorial", "fail")
        with pytest.raises(ValueError, match="independent_review_pending"):
            if stage == "begin":
                manual.manual_begin(db, row["manual_id"], owner=OWNER)
            else:
                manual.manual_complete(db, row["manual_id"], **completion(row))


def test_source_mutation_and_wrong_owner_cannot_import_receipt(tmp_path):
    with Store(tmp_path) as db:
        row = prepare(db)
        with pytest.raises(ValueError, match="manual_owner_mismatch"):
            manual.manual_begin(db, row["manual_id"], owner="other-operator")
        manual.manual_begin(db, row["manual_id"], owner=OWNER)
        with db.conn:
            db.conn.execute("UPDATE item_versions SET text='changed evidence'")
        with pytest.raises(ValueError, match="independent_review_pending|manual_review_packet_changed"):
            manual.manual_complete(db, row["manual_id"], **completion(row))


def test_manual_import_preserves_shadow_guard_and_automatic_send_is_blocked(tmp_path):
    with Store(tmp_path) as db:
        row = prepare(db)
        (tmp_path / "restore-state.json").write_text(
            json.dumps(
                {
                    "guard_version": 1,
                    "state": "ready",
                    "mode": "shadow",
                    "writer_enabled": False,
                    "sender_enabled": False,
                    "ownership": "unclaimed",
                    "source_sha256": "a" * 64,
                    "validation": {"status": "valid"},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="restore_shadow_only"):
            delivery.send(
                db,
                row["delivery_key"],
                lambda *args: pytest.fail("external transport invoked"),
                owner_epoch=OWNER,
            )
        assert manual.manual_begin(db, row["manual_id"], owner=OWNER)["manual_dispatch_allowed"]
        manual.manual_complete(db, row["manual_id"], **completion(row))
        with pytest.raises(ValueError, match="restore_shadow_only"):
            assert_runtime_permission(tmp_path, "send")


def test_publication_failure_rolls_back_sent_outbox_and_manual_receipt(tmp_path, monkeypatch):
    with Store(tmp_path) as db:
        row = prepare(db)
        manual.manual_begin(db, row["manual_id"], owner=OWNER)

        def fail(*args, **kwargs):
            raise ValueError("fixture_publication_failure")

        monkeypatch.setattr(editor, "record_publication", fail)
        with pytest.raises(ValueError, match="fixture_publication_failure"):
            manual.manual_complete(db, row["manual_id"], **completion(row))
        assert delivery.get(db, row["delivery_key"])["status"] == "unknown"
        assert manual.get(db, row["manual_id"])["status"] == "attempted"
        assert not db.conn.execute("SELECT * FROM report_publications").fetchone()


def test_prepared_manual_outbox_cannot_dispatch_through_automatic_sender(tmp_path):
    with Store(tmp_path) as db:
        row = prepare(db)
        result = delivery.send(
            db,
            row["delivery_key"],
            lambda *args: pytest.fail("automatic send must not take manual reservation"),
            owner_epoch=OWNER,
        )
        assert result["status"] == "manual_prepared"
        with db.conn:
            db.conn.execute("UPDATE outbox SET owner_epoch='different-owner'")
        with pytest.raises(ValueError, match="manual_outbox_changed"):
            manual.manual_begin(db, row["manual_id"], owner=OWNER)
        assert manual.get(db, row["manual_id"])["status"] == "prepared"


def test_explicit_unknown_and_conflicting_receipt_cannot_resend(tmp_path):
    with Store(tmp_path) as db:
        row = prepare(db)
        manual.manual_begin(db, row["manual_id"], owner=OWNER)
        assert manual.manual_unknown(db, row["manual_id"], owner=OWNER)["status"] == "unknown"
        assert not manual.manual_begin(db, row["manual_id"], owner=OWNER)["manual_dispatch_allowed"]
        args = completion(row, reconcile=True)
        manual.manual_complete(db, row["manual_id"], **args)
        args["connector_receipt"]["message_id"] = "another-message"
        with pytest.raises(ValueError, match="manual_receipt_conflict"):
            manual.manual_complete(db, row["manual_id"], **args)


def test_manual_mutator_does_not_implicitly_commit_callers_work(tmp_path):
    with Store(tmp_path) as db:
        row = prepare(db)
        db.conn.execute("INSERT INTO meta VALUES ('fixture-uncommitted','value')")
        with pytest.raises(ValueError, match="manual_requires_own_transaction"):
            manual.manual_begin(db, row["manual_id"], owner=OWNER)
        db.conn.rollback()
        assert not db.conn.execute("SELECT 1 FROM meta WHERE key='fixture-uncommitted'").fetchone()


@pytest.mark.parametrize("sent", [False, True])
def test_owner_drift_blocks_unknown_or_sent_reentry(tmp_path, sent):
    with Store(tmp_path) as db:
        row = prepare(db)
        manual.manual_begin(db, row["manual_id"], owner=OWNER)
        args = completion(row)
        if sent:
            manual.manual_complete(db, row["manual_id"], **args)
        with db.conn:
            db.conn.execute("UPDATE outbox SET owner_epoch='different-owner'")
        with pytest.raises(ValueError, match="manual_outbox_changed"):
            if sent:
                manual.manual_complete(db, row["manual_id"], **args)
            else:
                manual.manual_unknown(db, row["manual_id"], owner=OWNER)


@pytest.mark.parametrize(
    "sent_at,error",
    [
        ("2026-10-22T00:59:59Z", "manual_receipt_predates_attempt"),
        ("2026-10-22T01:00:00", "timezone_required"),
        ("2026-10-22T01:02:01Z", "manual_receipt_future_time"),
        ("2099-01-01T00:00:00Z", "manual_receipt_future_time"),
    ],
)
def test_invalid_receipt_time_cannot_journal_publication(tmp_path, monkeypatch, sent_at, error):
    monkeypatch.setattr(manual, "utcnow", lambda: "2026-10-22T01:00:00Z")
    with Store(tmp_path) as db:
        row = prepare(db)
        manual.manual_begin(db, row["manual_id"], owner=OWNER)
        args = completion(row)
        args["connector_receipt"]["sent_at"] = sent_at
        with pytest.raises(ValueError, match=error):
            manual.manual_complete(db, row["manual_id"], **args)
        assert manual.get(db, row["manual_id"])["status"] == "attempted"
        assert delivery.get(db, row["delivery_key"])["status"] == "unknown"
        assert not db.conn.execute("SELECT * FROM report_publications").fetchone()


@pytest.mark.parametrize(
    "sent_at,skew", [("2026-10-22T09:01:59+08:00", 119), ("2026-10-22T09:02:00+08:00", 120)]
)
def test_small_clock_skew_preserves_provider_time_but_immediate_digest_deduplicates(
    tmp_path, monkeypatch, sent_at, skew
):
    monkeypatch.setattr(manual, "utcnow", lambda: "2026-10-22T01:00:00Z")
    with Store(tmp_path) as db:
        row = prepare(db)
        manual.manual_begin(db, row["manual_id"], owner=OWNER)
        args = completion(row)
        args["connector_receipt"]["sent_at"] = sent_at
        result = manual.manual_complete(db, row["manual_id"], **args)
        assert result["receipt"]["sent_at"] == sent_at
        assert result["receipt"]["publication_clock_skew_seconds"] == skew
        assert result["receipt"]["publication_effective_at"] == "2026-10-22T01:00:00Z"
        assert (
            db.conn.execute("SELECT published_at FROM report_publications").fetchone()[0]
            == "2026-10-22T01:00:00Z"
        )
        report = editor.build_digest(db, "2026-10-21T00:00:00Z", "2026-10-22T01:00:00Z")
        assert report["cards"] == []
        monkeypatch.setattr(manual, "utcnow", lambda: "2026-10-22T01:03:00Z")
        assert manual.manual_complete(db, row["manual_id"], **args)["receipt"] == result["receipt"]
