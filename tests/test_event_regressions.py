"""Independent review regressions: time cutoffs and membership corrections."""

import pytest

from radar import events
from radar.events import event_evidence, list_events, rebuild
from radar.models import Item, Scan
from radar.store import Store

T1 = "2026-01-01T00:00:00Z"
T2 = "2026-01-02T00:00:00Z"
T3 = "2026-01-03T00:00:00Z"


def put(db, item, run):
    db.save_scan(Scan(item.source_id, [item], complete=True), run)


def test_comment_moved_to_missing_root_detaches_previous_membership(tmp_path):
    with Store(tmp_path) as db:
        post = Item("s", "p", "https://example.com/p", "AI release", "post", fetched_at=T1)
        comment = Item(
            "s",
            "c",
            "https://example.com/c",
            "",
            "comment",
            fetched_at=T1,
            kind="comment",
            parent_id="p",
            metadata={"root_id": "p"},
        )
        put(db, post, "post")
        put(db, comment, "comment")
        rebuild(db)
        comment.parent_id = "missing"
        comment.metadata["root_id"] = "missing"
        comment.fetched_at = T2
        put(db, comment, "moved")
        result = rebuild(db)
        assert result["orphan_comments"] == 1
        assert list_events(db)[0]["comment_count"] == 0
        assert (
            db.conn.execute(
                "SELECT event_id FROM event_members WHERE item_id=?", (comment.item_id,)
            ).fetchone()
            is None
        )


def test_tombstone_does_not_rewrite_pre_deletion_event_history(tmp_path):
    with Store(tmp_path) as db:
        first = Item("rss", "p1", "https://example.com/launch", "AI release", "original", fetched_at=T1)
        second = Item(
            "hn",
            "p2",
            "https://news.ycombinator.com/item?id=2",
            "AI release",
            "discussion",
            fetched_at=T2,
            metadata={"external_url": "https://example.com/launch"},
        )
        put(db, first, "first")
        put(db, second, "second")
        rebuild(db)
        second.kind = "tombstone"
        second.text = ""
        second.fetched_at = T3
        put(db, second, "deleted")
        rebuild(db)
        before_deletion = list_events(db, cutoff="2026-01-02T12:00:00Z")[0]
        assert before_deletion["item_count"] == 2
        current = list_events(db)[0]
        assert current["item_count"] == 1
        assert current["updated_at"] == T3


def test_queued_revisions_do_not_skip_historical_event_on_small_batch(tmp_path):
    with Store(tmp_path) as db:
        post = Item("s", "p", "https://example.com/p", "AI initial release", "old text", fetched_at=T1)
        put(db, post, "initial")
        post.title = "AI corrected release"
        post.text = "new text"
        post.fetched_at = T3
        put(db, post, "corrected")
        first_batch = rebuild(db, batch_size=1)
        assert first_batch["next_cursor"] == 1
        history = list_events(db, cutoff=T2)
        assert len(history) == 1
        assert history[0]["title"] == "AI initial release"
        rebuild(db, batch_size=1)
        assert list_events(db)[0]["title"] == "AI corrected release"


def test_legacy_evidence_does_not_attach_current_replacement_url(tmp_path):
    with Store(tmp_path) as db:
        post = Item("s", "p", "https://example.com/old", "AI release", "old text", fetched_at=T1)
        put(db, post, "legacy")
        rebuild(db)
        # Simulate a schema-2 item whose immutable context did not exist yet.
        db.conn.execute("DELETE FROM item_context_versions WHERE item_id=?", (post.item_id,))
        db.conn.commit()
        post.url = "https://example.com/replacement"
        post.fetched_at = T3
        put(db, post, "new-observation")
        rebuild(db)
        past = list_events(db, cutoff=T2)[0]
        legacy_evidence = event_evidence(db, past)[0]
        assert legacy_evidence["metadata"]["context_provenance"] == "legacy_unverified"
        # Unknown is acceptable; retroactively attaching today's URL is not.
        assert legacy_evidence.get("url") != "https://example.com/replacement"


def comment_item(native_id="c", observed=T1):
    return Item(
        "s",
        native_id,
        f"https://example.com/{native_id}",
        "",
        "comment",
        fetched_at=observed,
        kind="comment",
        parent_id="p",
        metadata={"root_id": "p"},
    )


@pytest.mark.parametrize("replacement_kind", ["tombstone", "comment"])
def test_root_stops_being_post_detaches_its_children_at_change_time(tmp_path, replacement_kind):
    with Store(tmp_path) as db:
        post = Item("s", "p", "https://example.com/p", "AI initial", "body", fetched_at=T1)
        put(db, post, "post")
        put(db, comment_item(), "comment")
        rebuild(db)
        post.kind, post.fetched_at = replacement_kind, T3
        put(db, post, "removed-root")
        result = rebuild(db)
        assert list_events(db) == []
        assert list_events(db, cutoff=T2)[0]["comment_count"] == 1
        assert result["orphan_comments"] == (2 if replacement_kind == "comment" else 1)
        assert db.conn.execute("SELECT count(*) FROM event_members").fetchone()[0] == 0
        last = db.conn.execute(
            "SELECT observed_at FROM event_versions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        assert last[0] == T3


def test_root_url_move_reassigns_processed_children_and_keeps_history(tmp_path):
    with Store(tmp_path) as db:
        post = Item("s", "p", "https://example.com/p", "AI initial", "body", fetched_at=T1)
        put(db, post, "post")
        put(db, comment_item(), "comment")
        rebuild(db)
        post.url, post.fetched_at = "https://example.com/new", T3
        put(db, post, "moved-root")
        rebuild(db)
        current = list_events(db)
        assert len(current) == 1
        assert current[0]["anchor"] == "https://example.com/new"
        assert current[0]["comment_count"] == 1
        assert list_events(db, cutoff=T2)[0]["anchor"] == "https://example.com/p"


def test_unprocessed_future_comment_is_excluded_from_root_projection(tmp_path):
    with Store(tmp_path) as db:
        put(db, Item("s", "p", "https://example.com/p", "AI", "body", fetched_at=T1), "post")
        put(db, comment_item(observed=T3), "future-comment")
        rebuild(db, batch_size=1)
        assert list_events(db)[0]["comment_count"] == 0
        rebuild(db, batch_size=1)
        assert list_events(db)[0]["comment_count"] == 1
        assert list_events(db, cutoff=T2)[0]["comment_count"] == 0


def test_event_sample_is_bounded_without_truncating_membership_or_counts(tmp_path):
    with Store(tmp_path) as db:
        items = [Item("s", "p", "https://example.com/p", "AI", "body", fetched_at=T1)]
        items.extend(comment_item(f"c{i}") for i in range(205))
        db.save_scan(Scan("s", items, complete=True), "large-thread")
        rebuild(db)
        event = list_events(db)[0]
        assert event["comment_count"] == 205 and event["item_count"] == 1
        assert event["member_count"] == 206 and event["member_sample_truncated"] is True
        assert len(event["members"]) == 200
        assert event["members"][0]["item_id"] == items[0].item_id
        assert db.conn.execute("SELECT count(*) FROM event_members").fetchone()[0] == 206
        assert rebuild(db)["changed_events"] == 0


def test_legacy_projection_cursor_requires_explicit_rebuild(tmp_path):
    with Store(tmp_path) as db:
        db.conn.execute("INSERT INTO meta VALUES ('event_cursor','1')")
        db.conn.commit()
        with pytest.raises(ValueError, match="event_state_migration_required"):
            rebuild(db)


def test_failed_projection_rolls_back_membership_state_and_cursor(tmp_path, monkeypatch):
    with Store(tmp_path) as db:
        put(db, Item("s", "p", "https://example.com/p", "AI", "body", fetched_at=T1), "post")

        def fail(*_):
            raise RuntimeError("forced_projection_failure")

        monkeypatch.setattr(events, "_project_event", fail)
        with pytest.raises(RuntimeError, match="forced_projection_failure"):
            rebuild(db)
        for table in ("event_members", "event_item_state", "event_versions", "events"):
            assert db.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert db.conn.execute("SELECT value FROM meta WHERE key='event_cursor'").fetchone() is None
