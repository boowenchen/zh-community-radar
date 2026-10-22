"""Independent runtime regressions with local fixtures and no external calls."""

import json

from test_editor import Provider, seed
from test_runtime import Runner, envelope

from radar import editor, events, recovery
from radar.llm import ModelRunner, quota_config
from radar.runtime import RuntimeConfig, run_tick
from radar.store import Store


def test_explicit_parked_analysis_can_finish_cursor_without_reporting_success(tmp_path):
    base = Runner(changed=True)

    def runner(stage, args, **kwargs):
        if stage == "analyze":
            return envelope(
                "analyze",
                {
                    "model_calls": 0,
                    "model_required": False,
                    "analysis_complete": True,
                    "dispositions": [
                        {"event_id": "fixture", "status": "parked", "reason": "quote_not_found"}
                    ],
                    "failures": [{"event_id": "fixture", "error": "quote_not_found"}],
                },
                "partial",
            )
        return base(stage, args, **kwargs)

    result = run_tick(RuntimeConfig(tmp_path, enable_model=True), runner=runner, now="2026-10-22T00:00:00Z")
    assert result["status"] == "partial" and result["last_completed_change_cursor"] == 4
    assert not result["pending_analysis"]
    assert result["analysis_disposition"]["dispositions"][0]["status"] == "parked"
    assert not result["notify"]


def test_unresolved_partial_analysis_cannot_advance_cursor(tmp_path):
    base = Runner(changed=True)

    def runner(stage, args, **kwargs):
        if stage == "analyze":
            return envelope("analyze", {"model_required": False, "analysis_complete": False}, "partial")
        return base(stage, args, **kwargs)

    result = run_tick(RuntimeConfig(tmp_path, enable_model=True), runner=runner, now="2026-10-22T00:00:00Z")
    assert result["status"] == "partial" and result["last_completed_change_cursor"] == 0
    assert result["pending_analysis"]


class LocalDataRunner:
    """Exercise real DB/candidate windows; the only provider is an in-memory fixture."""

    def __init__(self, folder, provider):
        self.folder, self.provider = folder, provider
        self.analysis_windows = []

    def __call__(self, stage, args, *, timeout, run_id):
        with Store(self.folder) as db:
            if stage == "collect":
                return envelope("collect", {"changed": False})
            if stage == "events":
                return envelope("events", events.rebuild(db))
            if stage == "changes":
                return envelope("changes", db.changes(int(args[args.index("--after") + 1]), 1000))
            if stage == "analyze":
                runner = ModelRunner(db, self.provider, quota_config(), run_id)
                since = args[args.index("--since") + 1] if "--since" in args else None
                self.analysis_windows.append(since)
                result = editor.analyze(db, runner, since=since, cutoff=args[args.index("--cutoff") + 1])
                return envelope("analyze", result, result["status"])
            if stage == "digest":
                return envelope("digest", editor.build_digest(db, cutoff=args[args.index("--cutoff") + 1]))
            if stage == "sources":
                return envelope("sources", {"coverage": db.source_status()})
            if stage == "outbox":
                return envelope("outbox", {"items": []})
        raise AssertionError(stage)


def test_pending_work_does_not_age_out_and_advance_cursor_without_analysis(tmp_path):
    with Store(tmp_path) as db:
        item, _ = seed(db)
    provider = Provider(item)
    runner = LocalDataRunner(tmp_path, provider)
    first = run_tick(RuntimeConfig(tmp_path), runner=runner, now="2026-10-22T00:00:00Z")
    assert first["pending_analysis"] and first["last_completed_change_cursor"] == 0
    later = run_tick(RuntimeConfig(tmp_path, enable_model=True), runner=runner, now="2026-10-24T00:00:00Z")
    assert provider.calls == 1 and runner.analysis_windows == [first["pending_since"]]
    assert later["last_completed_change_cursor"] == 1 and not later["pending_analysis"]
    assert later["pending_since"] is None
    assert later["analysis_disposition"]["analyzed"] == 1
    with Store(tmp_path) as db:
        report = json.loads(
            db.conn.execute(
                "SELECT document FROM reports WHERE report_id=?", (later["report_id"],)
            ).fetchone()[0]
        )
        assert report["cards"] == [], "Resolving old backlog must not present it as today's new report."


def test_explicit_old_bootstrap_exclusion_is_persisted_before_clearing_pending(tmp_path):
    with Store(tmp_path) as db:
        item, event = seed(db, published="2020-01-01T00:00:00Z")
    provider = Provider(item)
    runner = LocalDataRunner(tmp_path, provider)
    run_tick(RuntimeConfig(tmp_path), runner=runner, now="2026-10-22T00:00:00Z")
    later = run_tick(RuntimeConfig(tmp_path, enable_model=True), runner=runner, now="2026-10-24T00:00:00Z")
    assert provider.calls == 0 and not later["pending_analysis"]
    disposition = later["analysis_disposition"]
    assert disposition["excluded"] == [{"event_id": event["event_id"], "reason": "old_bootstrap"}]
    stored = json.loads((tmp_path / "runtime" / disposition["stage_receipt"]).read_text(encoding="utf-8"))
    assert stored["data"]["excluded"] == disposition["excluded"]
    state = json.loads((tmp_path / "runtime/state.json").read_text(encoding="utf-8"))
    assert state["last_analysis_disposition"] == disposition


def test_failed_health_reads_do_not_clear_known_gaps_and_unknown_delivery(tmp_path):
    normal = Runner()

    def first_runner(stage, args, **kwargs):
        if stage == "sources":
            return envelope("sources", {"coverage": [{"source_id": "s", "status": "blocked"}]})
        if stage == "outbox":
            return envelope("outbox", {"items": [{"status": "unknown"}]})
        return normal(stage, args, **kwargs)

    first = run_tick(RuntimeConfig(tmp_path), runner=first_runner, now="2026-10-22T00:00:00Z")
    assert first["active_gaps"] and first["outbox"]["unknown"] == 1

    def failed_reader(stage, args, **kwargs):
        if stage in {"sources", "outbox"}:
            return envelope(stage, {}, "failed", [{"code": "fixture_read_failure"}])
        return normal(stage, args, **kwargs)

    later = run_tick(RuntimeConfig(tmp_path), runner=failed_reader, now="2026-10-22T00:30:00Z")
    assert later["status"] == "failed"
    # Retain last-known facts or explicitly mark them unknown; zero/empty says resolved.
    assert later["outbox"]["unknown"] is None or later["outbox"]["unknown"] == 1
    assert later["active_gaps"] is None or later["active_gaps"] == first["active_gaps"]
    assert later["source_health"]["data_status"] == "stale"
    assert later["outbox"]["data_status"] == "stale"
    assert later["outbox"]["last_successful_read_at"] == first["outbox"]["last_successful_read_at"]
    recovered = run_tick(RuntimeConfig(tmp_path), runner=normal, now="2026-10-22T01:00:00Z")
    assert recovered["source_health"]["data_status"] == "fresh" and recovered["active_gaps"] == []
    assert recovered["outbox"]["data_status"] == "fresh" and recovered["outbox"]["unknown"] == 0


def test_first_health_read_failure_is_unknown_not_empty(tmp_path):
    normal = Runner()

    def failed_reader(stage, args, **kwargs):
        if stage in {"sources", "outbox"}:
            return envelope(stage, {}, "failed")
        return normal(stage, args, **kwargs)

    result = run_tick(RuntimeConfig(tmp_path), runner=failed_reader)
    assert result["active_gaps"] is None and result["source_health"]["data_status"] == "unknown"
    assert result["outbox"]["unknown"] is None and result["outbox"]["pending"] is None
    assert result["outbox"]["data_status"] == "unknown"


def test_unchanged_run_with_models_enabled_still_has_zero_model_work(tmp_path):
    runner = Runner()
    result = run_tick(RuntimeConfig(tmp_path, enable_model=True), runner=runner)
    assert result["model_calls"] == 0 and not result["notify"] and not result["pending_analysis"]
    assert "analyze" not in runner.calls and "digest" not in runner.calls


def test_real_cli_runner_restore_guard_prevents_collection_and_send(tmp_path):
    with Store(tmp_path / "source") as db:
        db.backup(tmp_path / "snapshot")
    target = tmp_path / "restored"
    recovery.restore_snapshot(tmp_path / "snapshot", target)
    # No enabled endpoints even if a regression accidentally bypasses the guard.
    config = tmp_path / "disabled-sources.json"
    config.write_text('{"schema_version":1,"sources":[]}', encoding="utf-8")
    result = run_tick(RuntimeConfig(target, source_config=config))
    assert result["status"] == "failed"
    assert {"stage": "collect", "code": "restore_shadow_only"} in result["errors"]
    assert result["model_calls"] == 0 and result["notify"] is False
    assert result["external_delivery"] == "not_attempted"
