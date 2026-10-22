import json

import portalocker
import pytest

from radar.runtime import RuntimeConfig, StageError, read_health, run_tick


def envelope(operation, data=None, status="success", errors=None):
    return {
        "schema_version": 1,
        "operation": operation,
        "status": status,
        "data": data or {},
        "errors": errors or [],
        "warnings": [],
        "usage": {"model_calls": 0},
    }


class Runner:
    def __init__(self, changed=False, collect_status="success", analyze_status="success", unknown_send=False):
        self.changed = changed
        self.collect_status = collect_status
        self.analyze_status = analyze_status
        self.unknown_send = unknown_send
        self.calls = []

    def __call__(self, stage, args, *, timeout, run_id):
        self.calls.append(stage)
        if stage == "collect":
            return envelope(
                "collect",
                {"changed": self.changed},
                self.collect_status,
                [{"code": "source_failed"}] if self.collect_status != "success" else [],
            )
        if stage == "events":
            return envelope("events", {"changed_events": int(self.changed), "pending_changes": 0})
        if stage == "changes":
            return envelope(
                "changes", {"changed": self.changed, "next_cursor": 4 if self.changed else 0, "events": []}
            )
        if stage == "analyze":
            return envelope(
                "analyze",
                {"model_calls": 1, "model_required": self.analyze_status != "success"},
                self.analyze_status,
            )
        if stage == "digest":
            return envelope(
                "digest",
                {
                    "report_id": "a" * 24,
                    "cards": [{"title": "candidate"}],
                    "notify": True,
                    "independent_review": "pending",
                },
            )
        if stage == "sources":
            return envelope("sources", {"coverage": [{"source_id": "x", "status": "ok"}]})
        if stage == "outbox":
            return envelope("outbox", {"items": [{"status": "unknown"}] if self.unknown_send else []})
        raise AssertionError(stage)


def test_unchanged_tick_never_calls_model_build_or_send(tmp_path):
    runner = Runner()
    result = run_tick(RuntimeConfig(tmp_path), runner=runner, now="2026-10-22T00:00:00Z")
    assert result["status"] == "success" and result["model_calls"] == 0
    assert not result["changed"] and not result["notify"]
    assert "analyze" not in runner.calls and "digest" not in runner.calls
    assert "send" not in runner.calls


def test_raw_snapshot_capture_is_explicit_and_boolean(tmp_path):
    seen = []
    delegate = Runner()

    def run(stage, args, **kwargs):
        if stage == "collect":
            seen.append(args)
        return delegate(stage, args, **kwargs)

    run_tick(RuntimeConfig(tmp_path, raw_cache=True), runner=run, now="2026-10-22T00:00:00Z")
    assert "--raw-cache" in seen[0]
    with pytest.raises(ValueError, match="runtime_flag_invalid"):
        RuntimeConfig(tmp_path, raw_cache="true").validate()


def test_new_work_default_shadow_no_models_and_cursor_not_lost(tmp_path):
    runner = Runner(changed=True)
    result = run_tick(RuntimeConfig(tmp_path), runner=runner, now="2026-10-22T00:00:00Z")
    assert result["mode"] == "shadow" and not result["notify"]
    assert result["model_calls"] == 0 and result["pending_analysis"]
    assert "analyze" not in runner.calls
    assert read_health(tmp_path, now="2026-10-22T00:01:00Z")["last_completed_change_cursor"] == 0


def test_candidate_analysis_does_not_approve_or_send_and_unknown_outbox_not_retried(tmp_path):
    runner = Runner(changed=True, unknown_send=True)
    result = run_tick(RuntimeConfig(tmp_path, enable_model=True), runner=runner, now="2026-10-22T00:00:00Z")
    assert result["model_calls"] == 1 and not result["notify"]
    assert result["review_status"] == "pending"
    assert result["outbox"]["unknown"] == 1 and "send" not in runner.calls
    assert result["last_completed_change_cursor"] == 4


def test_current_failure_does_not_hide_behind_last_success(tmp_path):
    cfg = RuntimeConfig(tmp_path, scheduled=True, interval_seconds=1800, missed_grace_seconds=600)
    run_tick(cfg, runner=Runner(), now="2026-10-22T00:00:00Z")
    result = run_tick(cfg, runner=Runner(collect_status="failed"), now="2026-10-22T00:30:00Z")
    assert result["status"] == "failed"
    health = read_health(tmp_path, now="2026-10-22T01:11:00Z")
    assert health["last_success_at"] == "2026-10-22T00:00:00Z"
    assert health["last_attempt_at"] == "2026-10-22T00:30:00Z"
    assert health["current_result"] == "failed" and health["missed_run"] is True


def test_manual_mode_has_no_fake_missed_schedule(tmp_path):
    run_tick(RuntimeConfig(tmp_path), runner=Runner(), now="2026-10-22T00:00:00Z")
    health = read_health(tmp_path, now="2026-11-22T00:00:00Z")
    assert health["missed_run"] is None and health["schedule_expectation"] == "manual"
    assert health["healthy"] is None


def test_same_host_lock_rejects_concurrent_run_without_touching_active_state(tmp_path):
    cfg = RuntimeConfig(tmp_path)
    folder = tmp_path / "runtime"
    folder.mkdir()
    state = folder / "state.json"
    state.write_text('{"active":"other-run"}', encoding="utf-8")
    with portalocker.Lock(folder / "tick.lock", timeout=0):
        result = run_tick(cfg, runner=Runner())
    assert result["errors"] == [{"code": "runtime_busy"}]
    assert json.loads(state.read_text()) == {"active": "other-run"}


def test_timed_out_mutating_stage_not_blindly_retried(tmp_path):
    runner = Runner()
    original = runner.__call__
    calls = []

    def timeout_once(stage, args, *, timeout, run_id):
        calls.append(stage)
        if stage == "collect":
            raise StageError("stage_timeout")
        return original(stage, args, timeout=timeout, run_id=run_id)

    result = run_tick(RuntimeConfig(tmp_path), runner=timeout_once)
    assert result["status"] == "failed"
    assert calls.count("collect") == 1
    assert "sources" in calls and "outbox" in calls


def test_proposal_runtime_configuration_rejected(tmp_path):
    with pytest.raises(ValueError, match="runtime_proposal_only"):
        RuntimeConfig.from_json({"schema_version": 1, "proposal_only": True, "data_dir": str(tmp_path)})
