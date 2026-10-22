"""One bounded, portable shadow tick. This module never sends notifications or installs a scheduler."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import portalocker
import psutil

from .models import json_text, utcnow
from .provenance import ORIGINS


class StageError(ValueError):
    pass


@dataclass
class RuntimeConfig:
    data_dir: Path
    source_config: Path = Path("config/sources.local.json")
    model_config: Path = Path("config/model.local.json")
    ledger_dir: Path | None = None
    python_executable: str = sys.executable
    shadow: bool = True
    enable_model: bool = False
    scheduled: bool = False
    raw_cache: bool = False
    data_origin: str = "unknown"
    interval_seconds: int = 1800
    missed_grace_seconds: int = 600
    tick_timeout_seconds: int = 600
    stage_timeout_seconds: int = 270
    max_requests: int = 200
    collect_seconds: int = 240
    analysis_limit: int = 4

    def validate(self):
        if self.data_origin not in ORIGINS:
            raise ValueError("runtime_data_origin_invalid")
        for flag in (self.shadow, self.enable_model, self.scheduled, self.raw_cache):
            if type(flag) is not bool:
                raise ValueError("runtime_flag_invalid")
        if not self.shadow:
            raise ValueError("runtime_live_delivery_not_implemented")
        limits = {
            "interval_seconds": (60, 86400),
            "missed_grace_seconds": (0, 86400),
            "tick_timeout_seconds": (10, 3600),
            "stage_timeout_seconds": (1, 600),
            "max_requests": (1, 1000),
            "collect_seconds": (1, 600),
            "analysis_limit": (1, 12),
        }
        for key, (low, high) in limits.items():
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("runtime_limit_invalid")
        if self.collect_seconds >= self.stage_timeout_seconds:
            raise ValueError("runtime_collect_timeout_margin_required")
        self.data_dir = Path(self.data_dir).resolve()
        self.source_config = Path(self.source_config).resolve()
        self.model_config = Path(self.model_config).resolve()
        self.ledger_dir = Path(self.ledger_dir).resolve() if self.ledger_dir else self.data_dir
        return self

    @classmethod
    def from_json(cls, value):
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("runtime_config_schema")
        if value.get("proposal_only"):
            raise ValueError("runtime_proposal_only")
        allowed = {field.name for field in fields(cls)}
        if set(value) - allowed - {"schema_version", "proposal_only"} or "data_dir" not in value:
            raise ValueError("runtime_config_fields")
        return cls(**{key: child for key, child in value.items() if key in allowed}).validate()


def _atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json_text(value), encoding="utf-8", newline="\n")
    temporary.replace(path)


def _instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result.astimezone(UTC)


def _state(data_dir):
    path = Path(data_dir) / "runtime" / "state.json"
    if not path.exists():
        return {
            "schema_version": 1,
            "last_completed_change_cursor": 0,
            "last_attempt_at": None,
            "last_success_at": None,
            "current_result": "not_run",
            "pending_analysis": False,
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("runtime_state_invalid") from None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("runtime_state_invalid")
    return value


def read_health(data_dir, now=None):
    """Read-only monitor input for Radar status; it does not replace external monitoring."""
    state = _state(data_dir)
    instant = _instant(now or utcnow())
    expected = state.get("scheduled", False)
    last = state.get("last_attempt_at")
    missed = None
    if expected and last:
        deadline = _instant(last) + timedelta(
            seconds=state["interval_seconds"] + state["missed_grace_seconds"]
        )
        missed = instant > deadline
    running_deadline = state.get("running_deadline_at")
    interrupted = (
        state.get("current_result") == "running"
        and bool(running_deadline)
        and instant > _instant(running_deadline)
    )
    return {
        **state,
        "observed_at": now or utcnow(),
        "schedule_expectation": "scheduled" if expected else "manual",
        "missed_run": missed,
        "interrupted_or_overdue": bool(interrupted),
        "last_run_successful": state.get("current_result") == "success",
        "healthy": (state.get("current_result") == "success" and missed is not True and not interrupted)
        if expected
        else None,
        "monitor_scope": "radar_only_external_observer_still_required",
        "cross_host_fencing": "not_provided",
    }


def _kill_owned_process_tree(process):
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
    except psutil.NoSuchProcess:
        pass
    process.wait(timeout=5)


class CLIRunner:
    def __init__(self, config):
        self.config = config

    def __call__(self, stage, args, *, timeout, run_id):
        cfg = self.config
        command = [
            cfg.python_executable,
            "-m",
            "radar",
            "--data-dir",
            str(cfg.data_dir),
            "--ledger-dir",
            str(cfg.ledger_dir),
            "--config",
            str(cfg.source_config),
            "--run-id",
            f"{run_id}-{stage}",
            "--idempotency-key",
            f"tick:{run_id}:{stage}",
            *args,
        ]
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                command,
                stdout=output,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_owned_process_tree(process)
                raise StageError("stage_timeout") from None
            if output.tell() > 4_000_000:
                raise StageError("stage_output_limit")
            output.seek(0)
            try:
                value = json.loads(output.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeError):
                raise StageError("stage_json_invalid") from None
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("operation") != args[0]
        ):
            raise StageError("stage_schema_incompatible")
        expected = {"success": 0, "partial": 2, "failed": 1, "skipped": 3}.get(value.get("status"))
        if expected is None or process.returncode != expected or not isinstance(value.get("data"), dict):
            raise StageError("stage_exit_mismatch")
        return value


def run_tick(config, runner=None, now=None):
    config.validate()
    folder = config.data_dir / "runtime"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(folder / "tick.lock", timeout=0):
            return _run_locked(config, runner or CLIRunner(config), folder, now)
    except portalocker.exceptions.LockException:
        # The active run owns state.json. A competing run must not overwrite its status.
        return {
            "schema_version": 1,
            "status": "failed",
            "errors": [{"code": "runtime_busy"}],
            "changed": False,
            "notify": False,
            "model_calls": 0,
            "mode": "shadow",
        }


def _run_locked(config, runner, folder, now):
    state = _state(config.data_dir)
    previous_attempt = state.get("last_attempt_at")
    had_pending = bool(state.get("pending_analysis"))
    pending_since = state.get("pending_since")
    run_id = uuid.uuid4().hex
    started_at = now or utcnow()
    started = time.monotonic()
    clock = lambda: now or utcnow()
    previous_incomplete = state.get("current_result") == "running"
    state.update(
        {
            "run_id": run_id,
            "last_attempt_at": started_at,
            "current_result": "running",
            "scheduled": config.scheduled,
            "interval_seconds": config.interval_seconds,
            "missed_grace_seconds": config.missed_grace_seconds,
            "mode": "shadow",
            "running_deadline_at": (
                _instant(started_at) + timedelta(seconds=config.tick_timeout_seconds)
            ).isoformat(),
            "phase": "starting",
            "previous_incomplete_run": previous_incomplete,
        }
    )
    _atomic_json(folder / "state.json", state)
    errors, stages = [], []
    status = "success"

    def invoke(stage, args):
        nonlocal status
        state["phase"] = stage
        _atomic_json(folder / "state.json", state)
        remaining = config.tick_timeout_seconds - (time.monotonic() - started)
        try:
            if remaining <= 0:
                raise StageError("tick_deadline")
            result = runner(stage, args, timeout=min(config.stage_timeout_seconds, remaining), run_id=run_id)
            if result.get("schema_version") != 1 or result.get("status") not in {
                "success",
                "partial",
                "failed",
                "skipped",
            }:
                raise StageError("stage_schema_incompatible")
            if not isinstance(result.get("data"), dict):
                raise StageError("stage_schema_incompatible")
        except (StageError, OSError, ValueError) as exc:
            code = str(exc) if isinstance(exc, StageError) else "stage_execution_error"
            result = {"schema_version": 1, "status": "failed", "data": {}, "errors": [{"code": code}]}
        stage_status = result["status"]
        if stage_status == "failed":
            status = "failed"
        elif stage_status != "success" and status == "success":
            status = "partial"
        for error in result.get("errors", []):
            errors.append({"stage": stage, "code": error.get("code", "stage_failed")})
        stages.append(
            {
                "stage": stage,
                "status": stage_status,
                "attempts": 1,
                "model_calls": result.get("data", {}).get("model_calls", 0),
            }
        )
        receipts = folder / "stages"
        receipts.mkdir(exist_ok=True)
        _atomic_json(receipts / f"{run_id}-{stage}.json", result)
        return result

    invoke(
        "collect",
        [
            "collect",
            "--due",
            "--data-origin",
            config.data_origin,
            "--max-requests",
            str(config.max_requests),
            "--max-seconds",
            str(config.collect_seconds),
            *(["--raw-cache"] if config.raw_cache else []),
        ],
    )
    events = invoke("events", ["events", "rebuild"])
    cursor = state.get("last_completed_change_cursor", 0)
    changes = invoke("changes", ["changes", "--after", str(cursor), "--limit", "1000"])
    changed = bool(changes["data"].get("changed"))
    pending = changed or state.get("pending_analysis", False)
    state["pending_analysis"] = pending
    if pending and not pending_since:
        # Fix the admission window until this work is resolved. Recomputing a
        # rolling day after an outage can silently age an unassessed event out.
        instants = [_instant(started_at)]
        instants.extend(
            _instant(row["observed_at"])
            for row in changes["data"].get("events", [])
            if row.get("observed_at")
        )
        if had_pending and previous_attempt:
            instants.append(_instant(previous_attempt))
        pending_since = (
            (min(instants) - timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
    state["pending_since"] = pending_since if pending else None
    _atomic_json(folder / "state.json", state)
    report_id, review_status, model_calls = None, "not_applicable", 0
    analysis_disposition = None
    complete_events = events["status"] == "success" and not events["data"].get("pending_changes")
    successful_analysis = not pending
    if pending and complete_events:
        cutoff = clock()
        if config.enable_model:
            analysis = invoke(
                "analyze",
                [
                    "analyze",
                    "--model-config",
                    str(config.model_config),
                    "--since",
                    pending_since,
                    "--cutoff",
                    cutoff,
                    "--limit",
                    str(config.analysis_limit),
                ],
            )
            model_calls += analysis["data"].get("model_calls", 0)
            analysis_disposition = {
                "stage_receipt": f"stages/{run_id}-analyze.json",
                "status": analysis["status"],
                "analyzed": analysis["data"].get("analyzed", 0),
                "cached": analysis["data"].get("cached", 0),
                "excluded": analysis["data"].get("excluded", []),
                "failures": analysis["data"].get("failures", []),
                "dispositions": analysis["data"].get("dispositions", []),
                "analysis_complete": analysis["data"].get("analysis_complete"),
            }
            state["last_analysis_disposition"] = analysis_disposition
            successful_analysis = (
                analysis["status"] in {"success", "partial"}
                and not analysis["data"].get("model_required", True)
                and (analysis["data"].get("analysis_complete") is True or analysis["status"] == "success")
            )
        else:
            errors.append({"stage": "analyze", "code": "models_disabled_pending_work"})
            if status == "success":
                status = "partial"
        # Rendering existing validated candidates is deterministic and does not call a model.
        digest = invoke("digest", ["digest", "build", "--cutoff", cutoff, "--limit", "8"])
        report_id = digest["data"].get("report_id")
        review_status = digest["data"].get("independent_review", "pending")
        if successful_analysis and digest["status"] == "success" and changes["status"] == "success":
            state["last_completed_change_cursor"] = max(cursor, changes["data"].get("next_cursor", cursor))
            state["pending_analysis"] = False
            state["pending_since"] = None
    elif pending:
        errors.append({"stage": "events", "code": "event_rebuild_incomplete"})
        if status == "success":
            status = "partial"
    sources = invoke("sources", ["sources", "status"])
    if sources["status"] == "success":
        active_gaps = [
            {"source_id": s["source_id"], "status": s["status"], "error": s.get("error")}
            for s in sources["data"].get("coverage", [])
            if s.get("status") not in {"ok", "no_new", "disabled"}
        ]
        source_health = {"data_status": "fresh", "last_successful_read_at": clock(), "read_result": "success"}
    else:
        active_gaps = state.get("active_gaps")
        source_health = {
            "data_status": "stale" if active_gaps is not None else "unknown",
            "last_successful_read_at": state.get("source_health", {}).get("last_successful_read_at"),
            "read_result": sources["status"],
        }
    if active_gaps and status == "success":
        status = "partial"
    outbox = invoke("outbox", ["outbox"])
    outgoing = outbox["data"].get("items", [])
    if outbox["status"] == "success":
        delivery_state = {
            "pending": sum(row.get("status") == "pending" for row in outgoing),
            "unknown": sum(row.get("status") in {"unknown", "sending"} for row in outgoing),
            "automatic_send": False,
            "scope": "up_to_100_cli_rows",
            "possibly_truncated": len(outgoing) >= 100,
            "data_status": "fresh",
            "last_successful_read_at": clock(),
            "read_result": "success",
        }
    else:
        previous_delivery = state.get("outbox", {})
        delivery_state = {
            "pending": previous_delivery.get("pending"),
            "unknown": previous_delivery.get("unknown"),
            "automatic_send": False,
            "scope": previous_delivery.get("scope", "up_to_100_cli_rows"),
            "possibly_truncated": previous_delivery.get("possibly_truncated"),
            "data_status": "stale" if previous_delivery else "unknown",
            "last_successful_read_at": previous_delivery.get("last_successful_read_at"),
            "read_result": outbox["status"],
        }
    unknown_delivery = delivery_state["unknown"]
    if unknown_delivery:
        errors.append({"stage": "outbox", "code": "delivery_outcome_unknown_no_retry"})
        if status == "success":
            status = "partial"
    if delivery_state["possibly_truncated"]:
        errors.append({"stage": "outbox", "code": "outbox_view_may_be_truncated"})
        if status == "success":
            status = "partial"
    finished_at = clock()
    if status == "success":
        state["last_success_at"] = finished_at
    state.update(
        {
            "current_result": status,
            "finished_at": finished_at,
            "phase": "complete",
            "running_deadline_at": None,
            "active_gaps": active_gaps,
            "source_health": source_health,
            "last_report_id": report_id or state.get("last_report_id"),
            "outbox": delivery_state,
            "model_state": "enabled_bounded" if config.enable_model else "disabled",
            "errors": errors,
        }
    )
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "operation": "runtime.tick",
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": "shadow",
        "changed": changed,
        "notify": False,
        "model_calls": model_calls,
        "pending_analysis": state["pending_analysis"],
        "pending_since": state["pending_since"],
        "last_completed_change_cursor": state["last_completed_change_cursor"],
        "review_status": review_status,
        "analysis_disposition": analysis_disposition,
        "report_id": report_id,
        "outbox": state["outbox"],
        "active_gaps": active_gaps,
        "source_health": source_health,
        "stages": stages,
        "errors": errors,
        "data_cost_usd": 0,
        "external_delivery": "not_attempted",
        "cross_host_fencing": "not_provided",
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    _atomic_json(folder / f"{run_id}.json", receipt)
    _atomic_json(folder / "state.json", state)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(prog="radar-runtime")
    parser.add_argument("--runtime-config")
    parser.add_argument("--data-dir", default=os.environ.get("RADAR_DATA_DIR", "var/radar"))
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = (
            RuntimeConfig.from_json(json.loads(Path(args.runtime_config).read_text(encoding="utf-8")))
            if args.runtime_config
            else RuntimeConfig(Path(args.data_dir)).validate()
        )
        result = read_health(config.data_dir) if args.health else run_tick(config)
        print(json_text(result))
        return {"success": 0, "partial": 2, "failed": 1}.get(
            result.get("status"), 0 if result.get("healthy") else 2
        )
    except (OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "runtime_io_error"
        print(json_text({"schema_version": 1, "status": "failed", "errors": [{"code": code}]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
