import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from radar.replay import baseline, build_windows, import_legacy, pair_candidates
from radar.store import Store


def legacy_db(path: Path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE items(uid TEXT,source TEXT,grp TEXT,role TEXT,platform TEXT,
                   title TEXT,url TEXT,canon_url TEXT,summary TEXT,published TEXT,seen_at TEXT,
                   refreshed_at TEXT,extra TEXT,fetch_path TEXT,fetched_at TEXT,license_class TEXT)""")
    con.execute(
        "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "abc",
            "hn-best",
            "hn",
            "signal",
            "hn",
            "Tool 2.0",
            "https://example.com/tool",
            "https://example.com/tool",
            "A legacy summary",
            "2028-01-01T00:00:00Z",
            "2026-10-11T10:00:00Z",
            "2026-10-12T11:00:00Z",
            '{"token":"never-import-me","author":"private"}',
            "secret-path",
            "2026-10-12T12:00:00Z",
            "public",
        ),
    )
    con.commit()
    con.close()


def test_import_preserves_seen_but_does_not_backdate_latest_summary(tmp_path):
    old = tmp_path / "old.db"
    legacy_db(old)
    before = hashlib.sha256(old.read_bytes()).hexdigest()
    manifest = import_legacy(old, tmp_path / "replay")
    assert manifest["imported"] == 1 and manifest["future_publication_count"] == 1
    assert hashlib.sha256(old.read_bytes()).hexdigest() == before
    with Store(tmp_path / "replay") as store:
        row = store.items()[0]
        assert row["first_seen_at"] == "2026-10-11T10:00:00Z"
        assert row["fetched_at"] == "2026-10-12T12:00:00Z"
        assert row["published_at"] is None
        assert row["metadata"]["content_scope"] == "legacy_summary"
        assert "never-import-me" not in json.dumps(row)
        assert "secret-path" not in json.dumps(row)
        assert store.conn.execute("SELECT observed_at FROM item_versions").fetchone()[0] == row["fetched_at"]
    assert import_legacy(old, tmp_path / "replay")["dataset_sha256"] == manifest["dataset_sha256"]


def test_import_manifest_without_database_is_not_success(tmp_path):
    old = tmp_path / "old.db"
    legacy_db(old)
    destination = tmp_path / "replay"
    import_legacy(old, destination)
    (destination / "radar.sqlite3").unlink()
    with pytest.raises(ValueError, match="legacy_destination_missing_database"):
        import_legacy(old, destination)


def test_import_changes_are_globally_monotonic_across_interleaved_sources(tmp_path):
    old = tmp_path / "old.db"
    legacy_db(old)
    with sqlite3.connect(old) as con:
        original = list(con.execute("SELECT * FROM items").fetchone())
        con.execute("DELETE FROM items")
        # Deliberately reverse UID/time order and interleave repeated source groups.
        rows = [
            ("a", "source-z", "14:00:00"),
            ("b", "source-a", "12:00:00"),
            ("c", "source-z", "12:00:00"),
            ("d", "source-a", "13:00:00"),
            ("e", "source-a", "12:00:00"),
        ]
        for uid, source, clock in rows:
            row = original.copy()
            row[0], row[1] = uid, source
            row[11] = row[14] = "2026-10-12T" + clock + "Z"
            con.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    destination = tmp_path / "replay"
    import_legacy(old, destination)
    with Store(destination) as store:
        changes = store.conn.execute(
            "SELECT c.observed_at,i.source_id,i.native_id FROM changes c JOIN items i USING(item_id) ORDER BY seq"
        ).fetchall()
        ordered = [tuple(row) for row in changes]
        assert ordered == sorted(ordered)
        runs = store.conn.execute("SELECT run_id,source_id,observed_at FROM source_runs").fetchall()
        assert len(runs) == 4 and len({row[0] for row in runs}) == 4
        assert all(row["first_seen_at"] == "2026-10-11T10:00:00Z" for row in store.items())
        contexts = store.conn.execute("SELECT context FROM item_context_versions").fetchall()
        assert all(json.loads(row[0])["first_seen_at"] == "2026-10-11T10:00:00Z" for row in contexts)
        assert (
            store.conn.execute(
                "SELECT count(*) FROM changes c JOIN item_versions v USING(item_id,revision) "
                "WHERE c.observed_at != v.observed_at"
            ).fetchone()[0]
            == 0
        )
    with sqlite3.connect(destination / "radar.sqlite3") as con:
        con.execute("UPDATE changes SET observed_at='2030-01-01T00:00:00Z' WHERE seq=1")
    with pytest.raises(ValueError, match="legacy_projection_not_monotonic"):
        import_legacy(old, destination)


def test_idempotent_import_rejects_old_or_corrupted_projection(tmp_path):
    old = tmp_path / "old.db"
    legacy_db(old)
    destination = tmp_path / "replay"
    import_legacy(old, destination)
    with sqlite3.connect(destination / "radar.sqlite3") as con:
        con.execute("DELETE FROM meta WHERE key='legacy_projection_version'")
    with pytest.raises(ValueError, match="legacy_projection_requires_rebuild"):
        import_legacy(old, destination)


def test_windows_use_real_distinct_observation_instants_without_inventing_days():
    rows = [{"item_id": str(i), "observed_at": f"2026-10-12T10:{i:02d}:00Z"} for i in range(20)]
    windows = build_windows(rows, 5)
    assert len(windows) == 5
    assert len({w["cutoff"] for w in windows}) == 5
    assert sum(w["newly_visible_count"] for w in windows) == 20
    assert all(w["cutoff"].startswith("2026-10-12") for w in windows)
    with pytest.raises(ValueError, match="insufficient_distinct_observation_times"):
        build_windows(rows[:2], 5)


def test_candidate_pairs_have_no_gold_labels_and_are_reproducible():
    rows = [
        {
            "item_id": str(i),
            "url": f"https://example.com/{i // 2}",
            "title": f"Model release {i}",
            "source_id": str(i % 3),
            "text": "test",
            "observed_at": "2026-10-12T10:00:00Z",
        }
        for i in range(12)
    ]
    first = pair_candidates(rows, 10)
    assert first == pair_candidates(rows, 10)
    assert len(first) == 10 and len({r["pair_id"] for r in first}) == 10
    assert all("same_event" not in r and "gold" not in r for r in first)
    assert all(r["split"] == "holdout" and r["review_status"] == "unlabeled" for r in first)


def test_baseline_missing_days_are_not_zero_and_future_days_are_excluded():
    days = [
        {"date": "2026-10-01", "status": "complete", "count": 10},
        {"date": "2026-10-02", "status": "missing", "count": None},
        {"date": "2026-10-03", "status": "complete", "count": 0},
        {"date": "2026-10-04", "status": "partial", "count": 100},
        {"date": "2026-10-22", "status": "complete", "count": 999},
    ]
    result = baseline(days, "2026-10-22")
    assert result["effective_days"] == 2
    assert result["mean_per_effective_day"] == 5
    assert result["provisional"] and result["missing_or_incomplete_days"] == 26
    assert result["excluded_current_or_future"] == 1


def test_baseline_requires_unique_days_and_full_28_days_for_nonprovisional():
    days = [{"date": f"2026-09-{i:02d}", "status": "complete", "count": 0} for i in range(1, 29)]
    result = baseline(days, "2026-09-29")
    assert not result["provisional"] and result["zero_baseline"]
    with pytest.raises(ValueError, match="duplicate_day"):
        baseline(days + days[:1], "2026-09-29")
