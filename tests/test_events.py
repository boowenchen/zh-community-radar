from radar.events import list_events, rebuild
from radar.models import Item, Scan
from radar.store import Store


def put(db, source, items, run):
    db.save_scan(Scan(source, items=items, complete=True), run)


def test_reposts_share_event_but_new_version_does_not(tmp_path):
    with Store(tmp_path) as db:
        put(
            db,
            "github",
            [Item("github", "a", "https://github.com/a/b/releases/tag/v1", "v1", "change")],
            "one",
        )
        put(
            db,
            "hn",
            [
                Item(
                    "hn",
                    "1",
                    "https://news.ycombinator.com/item?id=1",
                    "a/b v1",
                    "",
                    metadata={"external_url": "https://github.com/a/b/releases/tag/v1"},
                )
            ],
            "two",
        )
        put(
            db,
            "github",
            [Item("github", "b", "https://github.com/a/b/releases/tag/v2", "v2", "next")],
            "three",
        )
        assert rebuild(db)["changed_events"] == 2
        events = list_events(db)
        assert sorted(e["item_count"] for e in events) == [1, 2]
        assert rebuild(db)["changed_events"] == 0


def test_comment_revision_updates_event_once_and_no_author_count_invented(tmp_path):
    with Store(tmp_path) as db:
        post = Item("hn", "1", "https://news.ycombinator.com/item?id=1", "GPU inference", "body")
        comment = Item(
            "hn",
            "2",
            "https://news.ycombinator.com/item?id=2",
            "",
            "great",
            kind="comment",
            parent_id="1",
            metadata={"root_id": "1"},
        )
        put(db, "hn", [post, comment], "one")
        rebuild(db)
        event = list_events(db)[0]
        assert event["comment_count"] == 1 and event["independent_authors"] is None
        comment.text = "actually broken"
        put(db, "hn", [comment], "two")
        assert rebuild(db)["changed_events"] == 1
        assert list_events(db)[0]["revision"] == 2


def test_cutoff_does_not_include_future_observation(tmp_path):
    with Store(tmp_path) as db:
        post = Item(
            "x",
            "1",
            "https://example.com/a",
            "AI inference",
            "body",
            published_at="2026-01-01T00:00:00Z",
            fetched_at="2026-02-01T00:00:00Z",
        )
        put(db, "x", [post], "one")
        rebuild(db)
        assert list_events(db, cutoff="2026-01-15T00:00:00Z") == []


def test_historical_evidence_never_uses_corrected_future_metadata(tmp_path):
    from radar.events import event_evidence

    with Store(tmp_path) as db:
        post = Item(
            "s",
            "p",
            "https://example.com/one",
            "AI release",
            "text",
            published_at="2026-01-01T00:00:00Z",
            fetched_at="2026-01-02T00:00:00Z",
            metadata={"score": 3},
        )
        put(db, "s", [post], "r1")
        rebuild(db)
        post.published_at = "2026-01-03T00:00:00Z"
        post.metadata["score"] = 999
        post.fetched_at = "2026-01-04T00:00:00Z"
        put(db, "s", [post], "r2")
        rebuild(db)
        past = list_events(db, cutoff="2026-01-02T12:00:00Z")[0]
        evidence = event_evidence(db, past)[0]
        assert evidence["published_at"] == "2026-01-01T00:00:00Z"
        assert evidence["metadata"]["score"] == 3
        assert list_events(db)[0]["revision"] == 2


def test_large_database_is_processed_in_bounded_incremental_batches(tmp_path):
    with Store(tmp_path) as db:
        put(
            db,
            "s",
            [Item("s", str(n), f"https://example.com/{n}", "AI", "body") for n in range(10001)],
            "large",
        )
        first = rebuild(db, batch_size=500)
        assert first["processed_changes"] == 500 and first["pending_changes"] == 9501
        second = rebuild(db, batch_size=500)
        assert second["changed_events"] == 500 and second["next_cursor"] == 1000


def test_orphan_comment_attaches_when_root_arrives(tmp_path):
    with Store(tmp_path) as db:
        put(
            db,
            "s",
            [Item("s", "c", "https://example.com/c", "", "comment", kind="comment", parent_id="p")],
            "r1",
        )
        rebuild(db)
        put(db, "s", [Item("s", "p", "https://example.com/p", "AI", "post")], "r2")
        rebuild(db)
        assert list_events(db)[0]["comment_count"] == 1
