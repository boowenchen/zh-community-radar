import json

from radar.cli import main


def test_run_id_cannot_escape_receipt_directory(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "--run-id", "../escape", "sources", "status"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["errors"][0]["code"] == "invalid_run_id"
    assert not (tmp_path / "data").exists()


def test_backoff_is_respected_even_without_due(tmp_path, monkeypatch):
    from argparse import Namespace

    from radar.cli import ADAPTERS, collect
    from radar.models import Scan
    from radar.store import Store

    def forbidden(*args):
        raise AssertionError("upstream requested during backoff")

    monkeypatch.setitem(ADAPTERS, "feed", forbidden)
    cfg = {
        "sources": [
            {
                "id": "x",
                "enabled": True,
                "adapter": "feed",
                "family": "rss",
                "url": "https://example.com/feed",
                "interval_minutes": 30,
            }
        ]
    }
    with Store(tmp_path) as db:
        db.save_scan(
            Scan("x", status="rate_limited", error="http_429", retry_at="2099-01-01T00:00:00Z"), "r1"
        )
        result = collect(db, Namespace(sources=None, due=False, max_requests=10, max_seconds=2), cfg, "r2")
        assert result["status"] == "failed"
        assert result["data"]["coverage"][0]["deferred_reason"] == "upstream_backoff"


def test_usage_does_not_need_working_directory_model_config(tmp_path, monkeypatch, capsys):
    from radar.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["--data-dir", str(tmp_path / "data"), "usage"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["calls"] == 0
    assert result["data"]["data_cost_usd"] == 0


def test_legacy_feed_context_refresh_preserves_overlap_cursor(tmp_path, monkeypatch):
    from argparse import Namespace

    from radar.cli import ADAPTERS, collect
    from radar.models import Item, Scan
    from radar.store import Store

    cfg = {
        "sources": [
            {
                "id": "legacy",
                "enabled": True,
                "adapter": "feed",
                "family": "rss",
                "url": "https://example.com/feed",
                "interval_minutes": 30,
            }
        ]
    }
    observed = []

    def adapter(source, fetcher, checkpoint, known):
        observed.append(checkpoint)
        return Scan("legacy", complete=True)

    monkeypatch.setitem(ADAPTERS, "feed", adapter)
    with Store(tmp_path) as db:
        db.save_scan(
            Scan(
                "legacy",
                [Item("legacy", "x", "https://example.com/x", "title", "text")],
                complete=True,
                checkpoint={"etag": "old", "last_modified": "old", "head_ids": ["x"]},
            ),
            "seed",
        )
        db.conn.execute("DELETE FROM item_context_versions")
        db.conn.commit()
        collect(db, Namespace(sources=None, due=False, max_requests=2, max_seconds=1), cfg, "new")
    assert observed == [{"head_ids": ["x"]}]
