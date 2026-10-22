"""Bounded subscription-model calls; external text never receives tools or credentials.

Claude flags were checked against CLI 2.1.296 and the official CLI/headless/env-vars
docs. Safe mode preserves subscription OAuth; bare mode deliberately does not.
The CLI's dollar estimate is not an authoritative subscription bill.
"""

import copy
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .models import json_text, utcnow


class ModelError(ValueError):
    def __init__(self, code, *, usage=None):
        super().__init__(code)
        # Error codes are fixed local strings; never put provider bodies/credentials here.
        self.usage = usage


def quota_config():
    return {
        "schema_version": 1,
        "mode": "subscription_quota",
        "provider": "claude_cli",
        "model": "sonnet",
        "timeout_seconds": 150,
        "max_output_tokens": 2200,
        "max_input_bytes": 18000,
        "input_overhead_reservation": 4096,
        "max_attempts_per_request": 2,
        "limits": {
            "run": {"calls": 4, "input_tokens": 70000, "output_tokens": 8800},
            "day": {"calls": 12, "input_tokens": 100000, "output_tokens": 15000},
            "month": {"calls": 360, "input_tokens": 3000000, "output_tokens": 450000},
        },
        "data_cost_usd": 0,
        "paid_data_fallback": False,
    }


def load_config(path=None):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig")) if path else quota_config()
    return _validate_config(config)


def _validate_config(config):
    if config.get("proposal_only"):
        raise ModelError("model_config_proposal_only")
    if "validation_local_date" in config:
        value = config["validation_local_date"]
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ModelError("validation_date_required")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ModelError("validation_date_required") from None
    if config.get("schema_version") != 1 or config.get("mode") not in {"subscription_quota", "token_budget"}:
        raise ModelError("model_mode_unsupported")
    if config.get("provider") not in {"claude_cli", "openai_compatible"}:
        raise ModelError("model_provider_unsupported")
    if not isinstance(config.get("model"), str) or not config["model"].strip():
        raise ModelError("model_name_invalid")
    if config["model"] in {"MODEL_ALIAS_TO_CONFIRM", "your-model-name"}:
        raise ModelError("model_name_unverified")
    if config.get("data_cost_usd") != 0 or config.get("paid_data_fallback") is not False:
        raise ModelError("paid_data_not_allowed")
    for scope in ("run", "day", "month"):
        for metric in ("calls", "input_tokens", "output_tokens"):
            value = config.get("limits", {}).get(scope, {}).get(metric)
            if type(value) is not int or value < 1:
                raise ModelError("model_budget_invalid")
    bounds = {
        "timeout_seconds": (1, 600),
        "max_output_tokens": (256, 10000),
        "max_input_bytes": (1000, 100000),
        "input_overhead_reservation": (1024, 20000),
        "max_attempts_per_request": (1, 2),
    }
    for field, (low, high) in bounds.items():
        if type(config.get(field)) is not int or not low <= config[field] <= high:
            raise ModelError("model_config_invalid")
    if config["provider"] == "openai_compatible":
        _validate_http_config(config.get("http"))
    return config


SCHEMA = """
CREATE TABLE IF NOT EXISTS model_calls (
 call_id TEXT PRIMARY KEY, cache_key TEXT NOT NULL, run_id TEXT NOT NULL,
 category TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT NOT NULL, local_day TEXT NOT NULL, local_month TEXT NOT NULL,
 reserved_input INTEGER NOT NULL, reserved_output INTEGER NOT NULL,
 actual_input INTEGER, actual_output INTEGER, usage_basis TEXT NOT NULL,
 response TEXT, error TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS model_cache ON model_calls(cache_key,status);
CREATE INDEX IF NOT EXISTS model_day ON model_calls(local_day);
"""


def initialize(db):
    db.conn.executescript(SCHEMA)


def _budget_period(config, instant):
    if config.get("proposal_only"):
        raise ModelError("model_config_proposal_only")
    parsed = datetime.fromisoformat(instant)
    if parsed.tzinfo is None:
        raise ModelError("timezone_required")
    local = parsed.astimezone(ZoneInfo("Asia/Taipei"))
    day, month = local.strftime("%Y-%m-%d"), local.strftime("%Y-%m")
    if "validation_local_date" in config and config["validation_local_date"] != day:
        raise ModelError("budget_validation_date_expired")
    return day, month


def _check_budget(db, config, run_id, day, month, reserved_input, reserved_output):
    """Shared by internal and externally dispatched calls, inside a write transaction."""
    for scope, clause, value in (
        ("run", "run_id", run_id),
        ("day", "local_day", day),
        ("month", "local_month", month),
    ):
        total = db.conn.execute(
            f"SELECT count(*),coalesce(sum(coalesce(actual_input,reserved_input)),0),"
            f"coalesce(sum(coalesce(actual_output,reserved_output)),0) FROM model_calls WHERE {clause}=?",
            (value,),
        ).fetchone()
        for metric, used, new in zip(
            ("calls", "input_tokens", "output_tokens"),
            total,
            (1, reserved_input, reserved_output),
            strict=True,
        ):
            cap = config.get("limits", {}).get(scope, {}).get(metric)
            if type(cap) is not int or cap < 0:
                raise ModelError("model_budget_invalid")
            if used + new > cap:
                raise ModelError(f"budget_{scope}_{metric}")


@contextmanager
def _usage_transaction(db):
    # These APIs own their transaction: never accidentally commit a caller's other work.
    if db.conn.in_transaction:
        raise ModelError("usage_transaction_active")
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        yield


def _external_id(external_id):
    if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 512:
        raise ModelError("external_id_invalid")
    return "external-" + hashlib.sha256(external_id.encode()).hexdigest()[:24]


def _external_result(row, changed=False, dispatch_allowed=False):
    return {
        "call_id": row["call_id"],
        "status": row["status"],
        "changed": changed,
        "dispatch_allowed": dispatch_allowed,
        "usage_basis": row["usage_basis"],
        "reserved_input": row["reserved_input"],
        "reserved_output": row["reserved_output"],
    }


def reserve_external_usage(
    db, *, external_id, run_id, category, provider, model, config, reserved_input, reserved_output, now=None
):
    """Reserve one external call before dispatch, using the shared ledger's three caps.

    Only a newly committed reservation returns dispatch_allowed=True. Persist external_id
    before dispatch; any retry/restart must reuse it and must NOT dispatch again, even when
    the first process died before receiving a provider job id. An unknown call keeps its
    conservative reservation until an actual usage receipt is reconciled. No network call
    occurs here. The caller must pass the same ledger DB as ModelRunner, not its content DB.
    """
    call_id = _external_id(external_id)
    if category not in {"review", "embedding", "canary", "analysis", "repair"}:
        raise ModelError("usage_category_invalid")
    if not all(isinstance(value, str) and value.strip() for value in (run_id, provider, model)):
        raise ModelError("external_usage_identity_invalid")
    if not all(type(value) is int and value > 0 for value in (reserved_input, reserved_output)):
        raise ModelError("unknown_usage_reservation_required")
    if db.conn.in_transaction:
        raise ModelError("usage_transaction_active")
    initialize(db)
    with _usage_transaction(db):
        row = db.conn.execute("SELECT * FROM model_calls WHERE call_id=?", (call_id,)).fetchone()
        if row:
            expected = (
                "external:" + external_id,
                run_id,
                category,
                provider,
                model,
                reserved_input,
                reserved_output,
            )
            actual = tuple(
                row[field]
                for field in (
                    "cache_key",
                    "run_id",
                    "category",
                    "provider",
                    "model",
                    "reserved_input",
                    "reserved_output",
                )
            )
            if actual != expected:
                raise ModelError("external_usage_conflict")
            return _external_result(row)
        instant = now or utcnow()
        day, month = _budget_period(config, instant)
        _check_budget(db, config, run_id, day, month, reserved_input, reserved_output)
        db.conn.execute(
            "INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                call_id,
                "external:" + external_id,
                run_id,
                category,
                provider,
                model,
                "reserved",
                instant,
                day,
                month,
                reserved_input,
                reserved_output,
                None,
                None,
                "unknown",
                json_text({"external_reservation_version": 1}),
                None,
                None,
            ),
        )
        row = db.conn.execute("SELECT * FROM model_calls WHERE call_id=?", (call_id,)).fetchone()
        return _external_result(row, changed=True, dispatch_allowed=True)


def finish_external_usage(
    db,
    *,
    external_id,
    status="complete",
    input_tokens=None,
    output_tokens=None,
    receipt_ref=None,
    reconcile_unknown=False,
):
    """Account for a reserved call without dispatching anything or releasing unknown cost.

    Counts include provider cache/thinking accounting exactly once. Actual overruns are
    recorded honestly and block later reservations. receipt_ref is a local evidence pointer,
    not authentication: the operator verifies the provider receipt. Reconciling a previously
    finalized unknown call requires an explicit flag and an observed receipt. Callers own
    no ambient transaction; this function commits the accounting update atomically.
    """
    call_id = _external_id(external_id)
    if status not in {"complete", "failed", "unknown"}:
        raise ModelError("usage_status_invalid")
    if any(
        value is not None and (type(value) is not int or value < 0) for value in (input_tokens, output_tokens)
    ):
        raise ModelError("usage_invalid")
    observed = input_tokens is not None and output_tokens is not None
    if (input_tokens is None) != (output_tokens is None) or (status == "unknown" and observed):
        raise ModelError("usage_invalid")
    if receipt_ref is not None and (
        not isinstance(receipt_ref, str) or not receipt_ref.strip() or len(receipt_ref) > 2048
    ):
        raise ModelError("usage_receipt_invalid")
    if db.conn.in_transaction:
        raise ModelError("usage_transaction_active")
    initialize(db)
    with _usage_transaction(db):
        row = db.conn.execute("SELECT * FROM model_calls WHERE call_id=?", (call_id,)).fetchone()
        if row is None:
            raise ModelError("external_reservation_missing")
        metadata = json.loads(row["response"] or "{}")
        if metadata.get("external_reservation_version") != 1:
            raise ModelError("external_reservation_missing")
        desired = (status, input_tokens, output_tokens, receipt_ref)
        previous = (row["status"], row["actual_input"], row["actual_output"], metadata.get("receipt_ref"))
        if row["status"] != "reserved":
            if desired == previous:
                return _external_result(row)
            if not (reconcile_unknown and row["usage_basis"] == "unknown"):
                raise ModelError("external_usage_conflict")
            if not observed or not receipt_ref:
                raise ModelError("external_reconciliation_receipt_required")
            metadata["previous_finalization"] = {
                "status": row["status"],
                "usage_basis": row["usage_basis"],
                "receipt_ref": metadata.get("receipt_ref"),
                "finished_at": row["finished_at"],
            }
        metadata["receipt_ref"] = receipt_ref
        db.conn.execute(
            "UPDATE model_calls SET status=?,actual_input=?,actual_output=?,usage_basis=?,response=?,"
            "finished_at=? WHERE call_id=?",
            (
                status,
                input_tokens,
                output_tokens,
                "observed" if observed else "unknown",
                json_text(metadata),
                utcnow(),
                call_id,
            ),
        )
        row = db.conn.execute("SELECT * FROM model_calls WHERE call_id=?", (call_id,)).fetchone()
        return _external_result(row, changed=True)


def record_external_usage(
    db,
    *,
    external_id,
    run_id,
    category,
    provider,
    model,
    started_at,
    input_tokens=None,
    output_tokens=None,
    reserved_input=0,
    reserved_output=0,
    status="complete",
):
    """Import an external review/canary receipt once. Thinking included in output is not added twice.

    Unknown usage needs conservative reservations supplied by the caller; it is never
    represented as zero actual usage. External work has already happened, so an overrun is
    recorded honestly and prevents subsequent requests through the normal budget check.
    """
    initialize(db)
    if category not in {"review", "embedding", "canary", "analysis", "repair"}:
        raise ModelError("usage_category_invalid")
    if status not in {"complete", "failed", "reserved"}:
        raise ModelError("usage_status_invalid")
    for value in (input_tokens, output_tokens):
        if value is not None and (type(value) is not int or value < 0):
            raise ModelError("usage_invalid")
    if not all(type(v) is int and v >= 0 for v in (reserved_input, reserved_output)):
        raise ModelError("usage_invalid")
    observed = input_tokens is not None and output_tokens is not None
    if not observed and (reserved_input < 1 or reserved_output < 1):
        raise ModelError("unknown_usage_reservation_required")
    instant = datetime.fromisoformat(started_at)
    if instant.tzinfo is None:
        raise ModelError("timezone_required")
    local = instant.astimezone(ZoneInfo("Asia/Taipei"))
    call_id = "external-" + hashlib.sha256(external_id.encode()).hexdigest()[:24]
    values = (
        call_id,
        "external:" + external_id,
        run_id,
        category,
        provider,
        model,
        status,
        started_at,
        local.strftime("%Y-%m-%d"),
        local.strftime("%Y-%m"),
        reserved_input,
        reserved_output,
        input_tokens if observed else None,
        output_tokens if observed else None,
        "observed" if observed else "unknown",
        None,
        None,
        utcnow(),
    )
    previous = db.conn.execute("SELECT * FROM model_calls WHERE call_id=?", (call_id,)).fetchone()
    if previous:
        if tuple(previous)[:-1] != values[:-1]:
            raise ModelError("external_usage_conflict")
        return {"call_id": call_id, "changed": False}
    with db.conn:
        db.conn.execute("INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
    return {"call_id": call_id, "changed": True}


def _usage_counts(usage):
    if usage.get("basis") != "observed":
        return None, None, "unknown"
    values = (usage.get("input_tokens"), usage.get("output_tokens"))
    if not all(type(v) is int and v >= 0 for v in values):
        return None, None, "unknown"
    return *values, "observed"


class ModelRunner:
    def __init__(self, db, provider, config, run_id, now=None, ledger_db=None):
        self.db = ledger_db if ledger_db is not None else db
        initialize(self.db)
        self.provider, self.config, self.run_id = provider, copy.deepcopy(config), run_id
        # A test clock is explicit; production clocks are sampled on each reservation.
        self.now = now

    def request(self, system, payload, schema, category="analysis"):
        encoded = json_text(payload)
        total_bytes = len((system + encoded + json_text(schema)).encode("utf-8"))
        extra_input = getattr(self.provider, "additional_input_bytes", None)
        if extra_input is not None:
            total_bytes += extra_input(schema)
        if total_bytes > self.config["max_input_bytes"]:
            raise ModelError("model_input_limit")
        identity = [self.provider.name, self.provider.model, system, payload, schema]
        if getattr(self.provider, "cache_identity", None):
            identity.append(self.provider.cache_identity)
        key = hashlib.sha256(json_text(identity).encode()).hexdigest()
        row = self.db.conn.execute(
            "SELECT * FROM model_calls WHERE cache_key=? AND status='complete' ORDER BY started_at DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row:
            return {
                "call_id": row["call_id"],
                "content": json.loads(row["response"]),
                "cache_hit": True,
                "usage": {
                    "input_tokens": row["actual_input"],
                    "output_tokens": row["actual_output"],
                    "basis": row["usage_basis"],
                    "billing_usd": None,
                },
            }
        prior = self.db.conn.execute("SELECT status FROM model_calls WHERE cache_key=?", (key,)).fetchall()
        if any(r[0] == "reserved" for r in prior):
            # A process killed after sending a request might have been charged. Do not blindly resend it.
            raise ModelError("model_call_outcome_unknown")
        if len(prior) >= self.config["max_attempts_per_request"]:
            raise ModelError("model_attempt_limit")
        instant = self.now or utcnow()
        day, month = _budget_period(self.config, instant)
        # UTF-8 bytes are a conservative text-token reservation, plus CLI overhead. This is
        # an estimate, not a tokenizer claim; actual excess is charged and blocks later calls.
        reserved_input = total_bytes + self.config["input_overhead_reservation"]
        reserved_output = self.config["max_output_tokens"]
        call_id = uuid.uuid4().hex
        with _usage_transaction(self.db):
            _check_budget(self.db, self.config, self.run_id, day, month, reserved_input, reserved_output)
            self.db.conn.execute(
                "INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    key,
                    self.run_id,
                    category,
                    self.provider.name,
                    self.provider.model,
                    "reserved",
                    instant,
                    day,
                    month,
                    reserved_input,
                    reserved_output,
                    None,
                    None,
                    "unknown",
                    None,
                    None,
                    None,
                ),
            )
        try:
            response = self.provider.generate(system, payload, schema, reserved_output)
            content = response["content"]
            if not isinstance(content, dict):
                raise ModelError("model_json_object_required")
            input_tokens, output_tokens, basis = _usage_counts(response.get("usage", {}))
            with self.db.conn:
                self.db.conn.execute(
                    "UPDATE model_calls SET status='complete',actual_input=?,actual_output=?,usage_basis=?,response=?,finished_at=? WHERE call_id=?",
                    (input_tokens, output_tokens, basis, json_text(content), utcnow(), call_id),
                )
            return {
                "call_id": call_id,
                "content": content,
                "cache_hit": False,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "basis": basis,
                    "billing_usd": None,
                },
            }
        except (ModelError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            code = str(exc) if isinstance(exc, ModelError) else "model_response_invalid"
            failed_usage = getattr(exc, "usage", None) or {}
            input_tokens, output_tokens, basis = _usage_counts(failed_usage)
            with self.db.conn:
                self.db.conn.execute(
                    "UPDATE model_calls SET status='failed',error=?,finished_at=?,actual_input=?,"
                    "actual_output=?,usage_basis=? WHERE call_id=?",
                    (code, utcnow(), input_tokens, output_tokens, basis, call_id),
                )
            raise ModelError(code) from None

    def usage(self, scope="run"):
        instant = self.now or utcnow()
        local = datetime.fromisoformat(instant).astimezone(ZoneInfo("Asia/Taipei"))
        filters = {
            "run": ("run_id", self.run_id),
            "day": ("local_day", local.strftime("%Y-%m-%d")),
            "month": ("local_month", local.strftime("%Y-%m")),
        }
        if scope not in filters:
            raise ModelError("usage_scope_invalid")
        field, value = filters[scope]
        rows = self.db.conn.execute(f"SELECT * FROM model_calls WHERE {field}=?", (value,)).fetchall()
        unknown = sum(r["usage_basis"] != "observed" for r in rows)
        return {
            "scope": scope,
            "period": value,
            "calls": len(rows),
            "unknown_calls": unknown,
            "input_tokens": None if unknown else sum(r["actual_input"] for r in rows),
            "output_tokens": None if unknown else sum(r["actual_output"] for r in rows),
            "observed_input_tokens": sum(r["actual_input"] or 0 for r in rows),
            "observed_output_tokens": sum(r["actual_output"] or 0 for r in rows),
            "reserved_input_tokens": sum(r["reserved_input"] for r in rows if r["actual_input"] is None),
            "reserved_output_tokens": sum(r["reserved_output"] for r in rows if r["actual_output"] is None),
            "billing_usd": None,
            "billing_basis": (
                "subscription_quota_not_invoice"
                if self.config["mode"] == "subscription_quota"
                and {row["provider"] for row in rows} <= {"claude_cli"}
                else "model_token_budget_not_invoice"
            ),
            "data_cost_usd": 0,
        }


class ClaudeCLI:
    name = "claude_cli"

    def __init__(self, executable="claude", model="sonnet", timeout_seconds=150):
        self.executable, self.model, self.timeout_seconds = executable, model, timeout_seconds

    def command(self, system, max_output_tokens):
        command = [
            self.executable,
            "--print",
            "--safe-mode",
            "--restricted",
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--no-chrome",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--setting-sources",
            "",
            "--settings",
            '{"disableAllHooks":true,"enabledPlugins":{}}',
            "--model",
            self.model,
            "--effort",
            "low",
            "--max-turns",
            "1",
            "--output-format",
            "json",
            "--system-prompt",
            system,
        ]
        allowed = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "TMPDIR",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "PROGRAMDATA",
            "HOMEDRIVE",
            "HOMEPATH",
            "LANG",
            "LC_ALL",
            "TERM",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        env.update(
            {
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens),
                "CLAUDE_CODE_MAX_RETRIES": "0",
                "MAX_STRUCTURED_OUTPUT_RETRIES": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
                "CLAUDE_CODE_SAFE_MODE": "1",
            }
        )
        return command, env

    def generate(self, system, payload, schema, max_output_tokens):
        command, env = self.command(system, max_output_tokens)
        if not Path(self.executable).is_file() and not shutil.which(self.executable):
            raise ModelError("claude_cli_missing")
        # No --json-schema tool: one ordinary response, checked by Radar itself. This avoids
        # an invisible multi-turn structured-output repair loop consuming extra quota.
        request = json_text({"response_schema": schema, "untrusted_evidence": payload})
        with tempfile.TemporaryDirectory(prefix="radar-model-") as work:
            try:
                result = subprocess.run(
                    command,
                    input=request,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=work,
                    env=env,
                    timeout=self.timeout_seconds,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise ModelError("model_timeout") from None
        # Do not surface stderr or CLI result strings: auth errors can expose account metadata.
        if len(result.stdout.encode()) > 2_000_000:
            raise ModelError("model_response_too_large")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ModelError("model_envelope_invalid") from None
        if result.returncode or response.get("is_error") or response.get("subtype") != "success":
            raise ModelError("model_cli_failed")
        usage = response.get("usage", {})
        tool_usage = usage.get("server_tool_use", {})
        if any(type(v) is int and v > 0 for v in tool_usage.values()):
            raise ModelError("model_tool_boundary_violation")
        output = response.get("structured_output")
        if output is None:
            raw = response.get("result", "").strip()
            if raw.startswith("```json\n") and raw.endswith("\n```"):
                raw = raw[8:-4]
            try:
                output = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raise ModelError("model_json_invalid") from None
        count_fields = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        observed = all(type(usage.get(k, 0)) is int and usage.get(k, 0) >= 0 for k in count_fields)
        observed = observed and type(usage.get("output_tokens")) is int and "input_tokens" in usage
        return {
            "content": output,
            "usage": {
                "input_tokens": sum(usage.get(k, 0) for k in count_fields) if observed else None,
                "output_tokens": usage.get("output_tokens") if observed else None,
                "basis": "observed" if observed else "unknown",
            },
        }


PRIVATE_HTTP_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
    )
)


def _is_private_literal(host):
    """Plain HTTP is only for a fixed loopback/private/CGNAT IP literal, never a hostname."""
    address = ipaddress.ip_address(host)
    return any(address in network for network in PRIVATE_HTTP_NETWORKS)


def _validate_http_config(config):
    if not isinstance(config, dict):
        raise ModelError("model_http_config_invalid")
    if set(config) - {
        "endpoint",
        "allowed_endpoints",
        "api_key_env",
        "allow_private_http",
        "response_format",
        "max_tokens_parameter",
    }:
        raise ModelError("model_http_config_invalid")
    endpoint, allowed = config.get("endpoint"), config.get("allowed_endpoints")
    if not isinstance(endpoint, str) or not isinstance(allowed, list) or not allowed:
        raise ModelError("model_endpoint_invalid")
    if endpoint not in allowed:
        raise ModelError("model_endpoint_not_allowed")
    for value in allowed:
        if not isinstance(value, str) or any(character.isspace() for character in value):
            raise ModelError("model_endpoint_invalid")
        try:
            url = httpx.URL(value)
            if (
                url.userinfo
                or url.query
                or url.fragment
                or not url.host
                or not url.path.endswith("/chat/completions")
                or str(url) != value
            ):
                raise ValueError
            if url.scheme != "https" and (
                url.scheme != "http"
                or config.get("allow_private_http") is not True
                or not _is_private_literal(url.host)
            ):
                raise ValueError
        except (ValueError, httpx.InvalidURL):
            raise ModelError("model_endpoint_invalid") from None
    key_reference = config.get("api_key_env")
    if not isinstance(key_reference, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key_reference):
        raise ModelError("model_api_key_reference_invalid")
    if config.get("response_format") not in {"json_object", "json_schema"}:
        raise ModelError("model_response_format_invalid")
    if config.get("max_tokens_parameter", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
        raise ModelError("model_output_limit_parameter_invalid")


def _openai_usage(envelope):
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if all(type(value) is int and 0 <= value <= 1_000_000_000 for value in (incoming, outgoing)):
            total = usage.get("total_tokens")
            if total is None or (type(total) is int and total == incoming + outgoing):
                # Cached prompt and reasoning details are already included in these totals.
                return {"basis": "observed", "input_tokens": incoming, "output_tokens": outgoing}
    return {"basis": "unknown", "input_tokens": None, "output_tokens": None}


class OpenAICompatibleHTTP:
    """One tool-free chat completion over an operator-configured exact endpoint.

    Protocol: docs.litellm.ai/docs/completion/{json_mode,input,output}; HTTPX 0.28.1.
    JSON schema enforcement remains in the evidence editor; response_format is a request,
    never proof of valid output. No client retries, redirects, ambient proxies or fallback.
    Gateway-side retry/tool policy must separately be verified before enabling a deployment.
    """

    name = "openai_compatible"

    def __init__(self, config, *, transport=None):
        config = copy.deepcopy(_validate_config(config))
        if config["provider"] != self.name:
            raise ModelError("model_provider_unsupported")
        self.model = config["model"]
        self.timeout_seconds = config["timeout_seconds"]
        self.http_config = config["http"]
        # Injected transports are solely an in-process test seam, never a config option.
        self.transport = transport
        self.cache_identity = hashlib.sha256(json_text(self.http_config).encode()).hexdigest()

    def additional_input_bytes(self, schema):
        # The schema also appears in user content, already counted by ModelRunner.
        return len(json_text(schema).encode()) if self.http_config["response_format"] == "json_schema" else 0

    def generate(self, system, payload, schema, max_output_tokens):
        key = os.environ.get(self.http_config["api_key_env"])
        if not key:
            raise ModelError("model_api_key_missing")
        if not 8 <= len(key) <= 4096 or any(not 33 <= ord(character) <= 126 for character in key):
            raise ModelError("model_api_key_invalid")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 10000:
            raise ModelError("model_output_limit_invalid")
        response_format = {"type": self.http_config["response_format"]}
        if response_format["type"] == "json_schema":
            response_format["json_schema"] = {"name": "radar_response", "strict": True, "schema": schema}
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json_text({"response_schema": schema, "untrusted_evidence": payload}),
                },
            ],
            "response_format": response_format,
            "stream": False,
            "n": 1,
            self.http_config.get("max_tokens_parameter", "max_tokens"): max_output_tokens,
        }
        timeout = httpx.Timeout(
            self.timeout_seconds,
            connect=min(10, self.timeout_seconds),
            write=min(10, self.timeout_seconds),
            pool=min(5, self.timeout_seconds),
        )
        transport = self.transport or httpx.HTTPTransport(retries=0, trust_env=False)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with (
                httpx.Client(
                    transport=transport, trust_env=False, follow_redirects=False, timeout=timeout
                ) as client,
                client.stream(
                    "POST",
                    self.http_config["endpoint"],
                    json=body,
                    headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
                ) as response,
            ):
                if response.status_code != 200:
                    if response.status_code in {401, 403}:
                        raise ModelError("model_http_auth_failed")
                    if response.status_code in {408, 429} or response.status_code >= 500:
                        raise ModelError("model_provider_failed")
                    raise ModelError(f"model_http_{response.status_code}")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise ModelError("model_timeout")
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ModelError("model_response_too_large")
                    chunks.append(chunk)
                raw = b"".join(chunks)
        except httpx.TimeoutException:
            raise ModelError("model_timeout") from None
        except httpx.HTTPError:
            raise ModelError("model_http_transport_failed") from None
        try:
            envelope = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ModelError("model_envelope_invalid") from None
        if not isinstance(envelope, dict):
            raise ModelError("model_envelope_invalid")
        usage = _openai_usage(envelope)
        if key in json_text(envelope):
            raise ModelError("model_secret_in_response", usage=usage)
        choices = envelope.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ModelError("model_envelope_invalid", usage=usage)
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ModelError("model_envelope_invalid", usage=usage)
        if message.get("tool_calls") or message.get("function_call"):
            raise ModelError("model_tool_boundary_violation", usage=usage)
        if message.get("refusal"):
            raise ModelError("model_refusal", usage=usage)
        if choice.get("finish_reason") != "stop":
            raise ModelError("model_completion_incomplete", usage=usage)
        try:
            content = json.loads(message.get("content"))
        except (ValueError, TypeError):
            raise ModelError("model_json_invalid", usage=usage) from None
        if not isinstance(content, dict):
            raise ModelError("model_json_object_required", usage=usage)
        # The JSON string in message.content may itself contain escaped credential characters.
        if key in json_text(content):
            raise ModelError("model_secret_in_response", usage=usage)
        return {"content": content, "usage": usage}


def create_provider(config, *, transport=None):
    """Construct a configured adapter without network access or reading a credential."""
    config = _validate_config(config)
    if config["provider"] == "openai_compatible":
        return OpenAICompatibleHTTP(config, transport=transport)
    executable = os.environ.get("RADAR_CLAUDE_EXECUTABLE")
    if not executable:
        candidate = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
        executable = str(candidate) if candidate.exists() else "claude"
    return ClaudeCLI(executable=executable, model=config["model"], timeout_seconds=config["timeout_seconds"])
