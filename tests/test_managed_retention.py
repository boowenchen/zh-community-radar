"""Managed raw cache uses real archive bytes and never deletes logical evidence."""

import gzip
import hashlib
from argparse import Namespace
from pathlib import Path

import pytest

from radar.retention import ManagedRawCache, RawSnapshotObserver
from radar.store import Store

OLD = "2026-09-01T00:00:00Z"
NOW = "2026-10-22T00:00:00Z"


@pytest.fixture
def cache(tmp_path):
    with Store(tmp_path / "data") as db:
        yield ManagedRawCache(db)


def captured(cache, body=b"public fixture", **kwargs):
    result = cache.capture(
        body,
        source_id="fixture",
        observed_at=OLD,
        content_kind="mixed",
        archive_policy="archive_allowed",
        **kwargs,
    )
    cache.finish([result["artifact_id"]], parse_success=True)
    return result


def test_archive_resolves_after_bounded_prune_and_backup_restore(cache, tmp_path):
    body = b"original source\n" * 100
    item = captured(cache, body)
    assert cache.resolve(item["artifact_id"]) == body
    hot = cache.root / "raw/managed" / (item["sha256"] + ".raw")
    result = cache.prune([item["artifact_id"]], now=NOW)
    assert result["pruned_files"] == 1 and not hot.exists()
    assert cache.resolve(item["artifact_id"]) == body
    snapshot = tmp_path / "snapshot"
    cache.db.backup(snapshot)
    from radar.recovery import restore_snapshot

    restored = tmp_path / "restored"
    restore_snapshot(snapshot, restored)
    with Store(restored) as other:
        assert ManagedRawCache(other).resolve(item["artifact_id"]) == body


def test_dedup_is_content_based_and_recent_alias_protects_shared_hot_file(cache):
    first = captured(cache)
    later = cache.capture(
        b"public fixture",
        source_id="other",
        observed_at=NOW,
        content_kind="post",
        archive_policy="archive_allowed",
    )
    cache.finish([later["artifact_id"]], parse_success=True)
    assert cache.conn.execute("SELECT count(*) FROM raw_cache_blobs_v1").fetchone()[0] == 1
    result = cache.prune([first["artifact_id"]], now=NOW)
    assert result["pruned_files"] == 0
    assert result["entries"][0]["reason"] == "within_retention_window"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"parse_status": "unknown"}, "parse_not_verified"),
        ({"archive_policy": "unknown"}, "archive_policy_unknown"),
        ({"observed_at": "2026-10-23T00:00:00Z"}, "observation_time_in_future"),
    ],
)
def test_uncertain_managed_rows_are_kept(cache, change, reason):
    options = {
        "source_id": "fixture",
        "observed_at": OLD,
        "content_kind": "post",
        "archive_policy": "archive_allowed",
    }
    parse_status = change.get("parse_status", "success")
    options.update({key: value for key, value in change.items() if key != "parse_status"})
    item = cache.capture(b"keep", **options)
    if parse_status == "success":
        cache.finish([item["artifact_id"]], parse_success=True)
    result = cache.prune([item["artifact_id"]], now=NOW)
    assert result["entries"][0]["reason"] == reason
    assert result["pruned_files"] == 0


def test_pin_and_validation_root_prevent_hot_prune(cache, tmp_path):
    item = captured(cache)
    cache.pin(item["artifact_id"], reason="frozen replay evidence")
    assert cache.prune([item["artifact_id"]], now=NOW)["entries"][0]["reason"] == "pinned"
    with Store(tmp_path / "validation") as db:
        protected = ManagedRawCache(db)
        other = captured(protected)
        assert protected.prune([other["artifact_id"]], now=NOW)["entries"][0]["reason"] == "protected_root"


def test_corrupt_archive_and_corrupt_hot_are_never_pruned(cache):
    item = captured(cache)
    hot = cache.root / "raw/managed" / (item["sha256"] + ".raw")
    hot.write_bytes(b"changed")
    result = cache.prune([item["artifact_id"]], now=NOW)
    assert result["entries"][0]["reason"] == "raw_hot_hash_mismatch"
    hot.write_bytes(b"public fixture")
    with cache.conn:
        cache.conn.execute("UPDATE raw_cache_blobs_v1 SET payload=?", (gzip.compress(b"bad"),))
    result = cache.prune([item["artifact_id"]], now=NOW)
    assert result["entries"][0]["reason"] == "raw_archive_hash_mismatch"
    assert hot.exists()


def test_unknown_artifact_and_legacy_paths_are_not_adopted(cache):
    legacy = cache.root / "raw/legacy.txt"
    legacy.parent.mkdir(exist_ok=True)
    legacy.write_bytes(b"legacy")
    result = cache.prune(["a" * 64], now=NOW)
    assert result["entries"][0]["reason"] == "raw_artifact_unknown"
    assert legacy.read_bytes() == b"legacy"
    with pytest.raises(ValueError, match="raw_artifact_id_invalid"):
        cache.resolve("../../outside")


def test_existing_hash_named_file_is_not_adopted_even_when_content_matches(cache):
    body = b"legacy identical bytes"
    sha256 = hashlib.sha256(body).hexdigest()
    path = cache.root / "raw/managed" / (sha256 + ".raw")
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    with pytest.raises(ValueError, match="raw_hot_unmanaged"):
        captured(cache, body)
    row = cache.conn.execute("SELECT artifact_id FROM raw_cache_artifacts_v1").fetchone()
    cache.finish([row[0]], parse_success=True)
    result = cache.prune([row[0]], now=NOW)
    assert result["pruned_files"] == 0
    assert result["entries"][0]["reason"] == "raw_hot_unmanaged"
    assert path.read_bytes() == cache.resolve(row[0]) == body


def test_prune_batch_caps_and_interrupted_missing_hot_are_recoverable(cache):
    first, second = captured(cache, b"one"), captured(cache, b"two")
    result = cache.prune([first["artifact_id"], second["artifact_id"]], now=NOW, max_files=1)
    assert result["pruned_files"] == 1 and result["remaining"] == [second["artifact_id"]]
    hot = cache.root / "raw/managed" / (second["sha256"] + ".raw")
    hot.unlink()  # Crash after exact hot unlink but before catalog timestamp update.
    resumed = cache.prune([second["artifact_id"]], now=NOW)
    assert resumed["entries"][0]["reason"] == "archive_only"
    assert cache.resolve(second["artifact_id"]) == b"two"


def test_archive_budget_is_total_and_observer_warns_without_breaking_collection(tmp_path):
    with Store(tmp_path / "data") as db:
        cache = ManagedRawCache(db, max_archive_bytes=25)
        observer = RawSnapshotObserver(cache)
        observer.start_source("fixture")
        observer(bytes(range(128)), {"status": 200, "url": "https://invalid/?token=secret"})
        receipt = observer.finish(parse_success=True)
        assert receipt["artifact_ids"] == []
        assert receipt["warnings"] == ["raw_capture_unavailable:raw_archive_budget_exhausted"]
        assert "secret" not in str(receipt)
        assert cache.conn.execute("SELECT count(*) FROM raw_cache_blobs_v1").fetchone()[0] == 0


def test_capture_parse_failure_and_observer_budget_remain_explicit(cache):
    observer = RawSnapshotObserver(cache, max_files=1)
    observer.start_source("fixture", archive_policy="archive_allowed")
    observer(b"first", {"status": 200})
    observer(b"second", {"status": 200})
    result = observer.finish(parse_success=False)
    assert len(result["artifact_ids"]) == 1
    assert "raw_capture_unavailable:raw_run_budget_exhausted" in result["warnings"]
    assert (
        cache.prune(result["artifact_ids"], now="2027-01-01T00:00:00Z")["entries"][0]["reason"]
        == "parse_not_verified"
    )


def test_source_sampling_budget_keeps_capacity_for_later_families(cache):
    observer = RawSnapshotObserver(cache)
    observer.start_source("hn")
    for index in range(75):
        observer(f"HN {index}".encode(), {"status": 200})
    hn = observer.finish(parse_success=True)
    assert len(hn["artifact_ids"]) == 10
    assert hn["warnings"] == ["raw_capture_unavailable:raw_source_budget_exhausted"]
    for source in ("github", "rss"):
        observer.start_source(source)
        observer(source.encode(), {"status": 200})
        later = observer.finish(parse_success=True)
        assert len(later["artifact_ids"]) == 1 and later["warnings"] == []
    assert observer.files == 12  # Per-source resets do not reset global accounting.


def test_collect_opt_in_capture_reaches_archive_and_receipt(tmp_path, monkeypatch):
    from radar import cli
    from radar.models import Scan

    class FakeFetcher:
        def __init__(self, *args):
            self.requests, self.receipts, self.response_observer = 0, [], None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def adapter(source, fetcher, checkpoint, known):
        fetcher.requests += 1
        if fetcher.response_observer:
            fetcher.response_observer(b"fixture body", {"status": 200})
        return Scan(source["id"], status="no_new", complete=True)

    monkeypatch.setattr(cli, "Fetcher", FakeFetcher)
    monkeypatch.setitem(cli.ADAPTERS, "fixture", adapter)
    cfg = {
        "sources": [
            {"id": "fixture", "enabled": True, "adapter": "fixture", "family": "test", "interval_minutes": 30}
        ]
    }
    with Store(tmp_path / "data") as db:
        result = cli.collect(
            db,
            Namespace(sources=None, due=False, max_requests=10, max_seconds=2, raw_cache=True),
            cfg,
            "fixture",
        )
        raw = result["data"]["coverage"][0]["raw_cache"]
        assert len(raw["artifact_ids"]) == 1 and raw["warnings"] == []
        assert ManagedRawCache(db).resolve(raw["artifact_ids"][0]) == b"fixture body"


def test_archive_decompression_is_bounded_even_if_compressed_hash_matches(cache):
    item = captured(cache, b"tiny")
    compressed = gzip.compress(b"x" * 1_000_000)
    with cache.conn:
        cache.conn.execute(
            "UPDATE raw_cache_blobs_v1 SET payload=?,compressed_sha256=?",
            (compressed, hashlib.sha256(compressed).hexdigest()),
        )
    with pytest.raises(ValueError, match="raw_archive_hash_mismatch"):
        cache.resolve(item["artifact_id"])
    assert cache.prune([item["artifact_id"]], now=NOW)["pruned_files"] == 0


def test_manifest_corruption_and_reparse_hot_are_retained(cache, monkeypatch):
    from types import SimpleNamespace

    item = captured(cache)
    hot = cache.root / "raw/managed" / (item["sha256"] + ".raw")
    original = Path.lstat

    def reparse(path):
        info = original(path)
        if path == hot:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", reparse)
    assert cache.prune([item["artifact_id"]], now=NOW)["entries"][0]["reason"] == "raw_path_unsafe"
    monkeypatch.setattr(Path, "lstat", original)
    with cache.conn:
        cache.conn.execute("UPDATE raw_cache_artifacts_v1 SET manifest='{}'")
    assert cache.prune([item["artifact_id"]], now=NOW)["entries"][0]["reason"] == "raw_manifest_hash_mismatch"
    assert hot.exists()


def test_failed_archive_capture_warning_does_not_change_collect_success(tmp_path, monkeypatch):
    from radar import cli
    from radar.models import Scan

    class FakeFetcher:
        def __init__(self, *args):
            self.requests, self.receipts, self.response_observer = 0, [], None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def adapter(source, fetcher, checkpoint, known):
        fetcher.response_observer(b"body", {"status": 200})
        return Scan(source["id"], status="no_new", complete=True)

    def unavailable(*args, **kwargs):
        raise ValueError("raw_archive_budget_exhausted")

    monkeypatch.setattr(cli, "Fetcher", FakeFetcher)
    monkeypatch.setitem(cli.ADAPTERS, "fixture", adapter)
    monkeypatch.setattr(ManagedRawCache, "capture", unavailable)
    cfg = {"sources": [{"id": "fixture", "enabled": True, "adapter": "fixture", "family": "test"}]}
    with Store(tmp_path / "data") as db:
        result = cli.collect(
            db,
            Namespace(sources=None, due=False, max_requests=10, max_seconds=2, raw_cache=True),
            cfg,
            "fixture",
        )
        assert result["status"] == "success"
        raw = result["data"]["coverage"][0]["raw_cache"]
        assert raw["artifact_ids"] == [] and raw["warnings"]
