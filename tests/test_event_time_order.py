"""Independent temporal regressions; observation time is not publication time."""

import json
import sqlite3

import pytest

from radar import cli
from radar.events import list_events, rebuild
from radar.models import Item, Scan
from radar.replay import FIELDS, import_legacy
from radar.store import Store

PAST = "2026-10-21T00:00:00Z"
FUTURE = "2026-10-22T00:00:00Z"
CUTOFF = "2026-10-21T12:00:00Z"
ANCHOR = "https://example.com/shared"


def put(db, item, run_id):
    db.save_scan(Scan(item.source_id, [item], complete=True), run_id)


def assert_out_of_order_rejected_without_writes(db, batch_size=5000):
    """Reject before any projection/cursor write, including across batch boundaries."""
    tables = ("meta", "event_item_state", "event_members", "events", "event_versions")

    def snapshot():
        return {table: [tuple(row) for row in db.conn.execute(f"SELECT * FROM {table}")] for table in tables}

    before = snapshot()
    with pytest.raises(ValueError, match="^event_observation_order_invalid$"):
        rebuild(db, batch_size=batch_size)
    assert snapshot() == before


@pytest.mark.parametrize("split_batches", [False, True])
def test_reverse_observation_posts_fail_closed_before_projection(tmp_path, split_batches):
    with Store(tmp_path) as db:
        future = Item("a", "future", ANCHOR, "Future AI", "future-only text", fetched_at=FUTURE)
        past = Item("b", "past", ANCHOR, "Past AI", "past-only text", fetched_at=PAST)
        put(db, future, "future-first")
        if split_batches:
            rebuild(db)
        put(db, past, "past-second")
        assert_out_of_order_rejected_without_writes(db)


def test_future_orphan_cannot_enter_past_root_snapshot(tmp_path):
    with Store(tmp_path) as db:
        future = Item(
            "s",
            "c",
            "https://example.com/c",
            "",
            "future comment",
            fetched_at=FUTURE,
            kind="comment",
            parent_id="p",
            metadata={"root_id": "p"},
        )
        past = Item("s", "p", ANCHOR, "Past AI", "past root", fetched_at=PAST)
        put(db, future, "future-comment-first")
        put(db, past, "past-root-second")
        assert_out_of_order_rejected_without_writes(db)


def test_legacy_import_orders_changes_globally_across_sources(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE items (" + ",".join(f"{name} TEXT" for name in FIELDS) + ")")
        # uid order is a,c,b by observation; per-source sorting is also insufficient:
        # source a owns both the first and last time, source b owns the middle.
        for uid, source_id, observed in (
            ("a", "a", FUTURE),
            ("b", "a", PAST),
            ("c", "b", "2026-10-21T06:00:00Z"),
        ):
            row = {name: None for name in FIELDS}
            row.update(
                uid=uid,
                source=source_id,
                title="AI snapshot",
                url=ANCHOR,
                summary=f"snapshot {uid}",
                seen_at=observed,
                fetched_at=observed,
            )
            connection.execute(
                "INSERT INTO items VALUES (" + ",".join("?" for _ in FIELDS) + ")",
                tuple(row[name] for name in FIELDS),
            )
    import_legacy(source, tmp_path / "imported")
    with Store(tmp_path / "imported") as db:
        instants = [row[0] for row in db.conn.execute("SELECT observed_at FROM changes ORDER BY seq")]
        assert instants == sorted(instants)


@pytest.mark.parametrize(
    "observed,expected_count",
    [
        ("2026-10-21T09:00:00+08:00", 1),  # 01:00 UTC, already observed.
        ("2026-10-21T01:00:00-07:00", 0),  # 08:00 UTC, still future.
    ],
)
def test_item_offset_observations_use_instant_comparison(tmp_path, observed, expected_count):
    with Store(tmp_path) as db:
        put(db, Item("s", "p", ANCHOR, "AI", "body", fetched_at=observed), "offset")
        rebuild(db)
        assert len(list_events(db, cutoff="2026-10-21T02:00:00Z")) == expected_count


def test_event_cli_normalizes_offset_cutoff(tmp_path, capsys):
    with Store(tmp_path) as db:
        put(db, Item("s", "p", ANCHOR, "AI", "body", fetched_at="2026-10-21T02:00:00Z"), "post")
        rebuild(db)
    code = cli.main(["--data-dir", str(tmp_path), "events", "list", "--cutoff", "2026-10-21T09:00:00+08:00"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["data"]["events"] == []  # Cutoff means 01:00 UTC, before observation.


def test_old_publication_is_valid_when_observation_order_is_monotonic(tmp_path):
    with Store(tmp_path) as db:
        put(db, Item("s", "first", ANCHOR, "AI first", "body", published_at=PAST, fetched_at=PAST), "first")
        put(
            db,
            Item(
                "t",
                "late",
                ANCHOR,
                "AI delayed",
                "late body",
                published_at="2020-01-01T00:00:00Z",
                fetched_at=FUTURE,
            ),
            "late",
        )
        assert rebuild(db)["processed_changes"] == 2
        assert list_events(db, cutoff=CUTOFF)[0]["item_count"] == 1
        assert list_events(db, cutoff=FUTURE)[0]["item_count"] == 2
