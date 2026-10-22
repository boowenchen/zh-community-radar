import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import portalocker

from . import __version__, delivery, editor, llm
from .events import event_evidence, list_events, rebuild
from .fetch import Fetcher, FetchError
from .models import Scan, json_text, utcnow
from .profiling import ResourceSample, disk_bytes
from .provenance import ORIGINS, begin_collection, finish_collection
from .recovery import assert_runtime_permission, atomic_write_json, restore_snapshot, validate_snapshot
from .sources import ADAPTERS
from .store import Store


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config.get("schema_version") != 1:
        raise ValueError("config_schema_incompatible")
    ids = set()
    for source in config["sources"]:
        if source["id"] in ids or source["adapter"] not in ADAPTERS:
            raise ValueError("duplicate_source_or_unsupported_adapter")
        ids.add(source["id"])
        if source.get("data_cost") != 0 or source.get("paid_fallback"):
            raise ValueError("zero_data_cost_required")
        if source.get("egress_policy", "direct") != "direct":
            raise ValueError("egress_not_yet_verified")
    return config


def collect(db, args, config, run_id):
    selected = set(args.sources.split(",")) if args.sources else None
    if selected and selected - {s["id"] for s in config["sources"]}:
        raise ValueError("unknown_source")
    provenance = begin_collection(
        config,
        getattr(args, "config", None),
        run_id=run_id,
        data_origin=getattr(args, "data_origin", None),
        due_only=args.due,
    )
    sources = [s for s in config["sources"] if s.get("enabled") and (not selected or s["id"] in selected)]
    states = {s["source_id"]: s for s in db.source_status()}
    receipts = []
    now = datetime.now(UTC)
    permitted = []
    for source in sources:
        state = states.get(source["id"], {})
        retry_at = state.get("next_allowed_at")
        if retry_at and datetime.fromisoformat(retry_at) > now:
            receipts.append(
                {
                    "source_id": source["id"],
                    "family": source["family"],
                    "status": state["status"],
                    "complete": False,
                    "observed": 0,
                    "changed": 0,
                    "error": state["error"],
                    "requests": 0,
                    "next_allowed_at": retry_at,
                    "deferred_reason": "upstream_backoff",
                }
            )
        else:
            permitted.append(source)
    sources = permitted
    if args.due:
        now = datetime.now(UTC)
        sources = [
            s
            for s in sources
            if s["id"] not in states
            or (now - datetime.fromisoformat(states[s["id"]]["last_attempt_at"])).total_seconds()
            >= s["interval_minutes"] * 60
        ]
    hosts = {urlsplit(s["url"]).hostname for s in sources if s.get("url")}
    for source in sources:
        hosts.update(source.get("allowed_hosts", []))
    raw_observer = None
    raw_warning = None
    if getattr(args, "raw_cache", False):
        from .retention import ManagedRawCache, RawSnapshotObserver

        try:
            raw_observer = RawSnapshotObserver(ManagedRawCache(db))
        except (ValueError, OSError, sqlite3.Error, portalocker.exceptions.LockException):
            raw_warning = "raw_capture_unavailable:raw_cache_initialization_failed"
    agent = os.environ.get("RADAR_USER_AGENT") or config.get("user_agent")
    fetch_options = {"user_agent_value": agent} if agent else {}
    with Fetcher(hosts, args.max_requests, args.max_seconds, **fetch_options) as fetcher:
        for source in sources:
            if raw_observer:
                raw_options = source.get("raw_cache", {})
                if not isinstance(raw_options, dict):
                    raw_options = {}
                raw_observer.start_source(
                    source["id"],
                    content_kind=raw_options.get("content_kind", "mixed"),
                    archive_policy=raw_options.get("archive_policy", "unknown"),
                )
                fetcher.response_observer = raw_observer
            before = fetcher.requests
            known = {
                r[0]
                for r in db.conn.execute("SELECT native_id FROM items WHERE source_id=?", (source["id"],))
            }
            checkpoint = db.checkpoint(source["id"])
            legacy_context = db.conn.execute(
                """SELECT 1 FROM items i
                WHERE source_id=? AND NOT EXISTS (SELECT 1 FROM item_context_versions c
                WHERE c.item_id=i.item_id AND c.revision=i.revision) LIMIT 1""",
                (source["id"],),
            ).fetchone()
            if source["adapter"] == "feed" and legacy_context:
                # A 304 cannot supply the missing immutable context. Refresh only when
                # normally due; preserve overlap head IDs and rate-limit decisions.
                checkpoint = {k: v for k, v in checkpoint.items() if k not in {"etag", "last_modified"}}
            try:
                scan = ADAPTERS[source["adapter"]](source, fetcher, checkpoint, known)
            except FetchError as error:
                scan = Scan(source["id"], status=error.status, error=error.code)
                scan.retry_at = getattr(error, "retry_at", None)
            except (ValueError, KeyError, TypeError, OverflowError):
                scan = Scan(source["id"], status="parse_error", error="invalid_source_payload")
            scan.requests = fetcher.requests - before
            scan.host = config.get("host_role", platform.node())
            receipt = db.save_scan(scan, run_id)
            receipt["family"] = source["family"]
            if raw_observer:
                receipt["raw_cache"] = raw_observer.finish(
                    parse_success=bool(scan.complete and scan.status in {"ok", "no_new", "stale"})
                )
            elif raw_warning:
                receipt["raw_cache"] = {"artifact_ids": [], "warnings": [raw_warning]}
            receipts.append(receipt)
            print(f"{source['id']}: {receipt['status']}, {receipt['observed']} items", file=sys.stderr)
        http_receipts = fetcher.receipts
    changed = sum(r["changed"] for r in receipts)
    errors = [r for r in receipts if r["status"] not in {"ok", "no_new"}]
    status = (
        "partial"
        if errors and any(r["observed"] or r["complete"] for r in receipts)
        else "failed"
        if errors
        else "success"
    )
    return {
        "status": status,
        "data": {
            "changed": bool(changed),
            "changed_items": changed,
            "events": [],
            "model_required": bool(changed),
            "notify": False,
            "coverage": receipts,
            "http": http_receipts,
            "due_only": args.due,
            "collection_provenance": finish_collection(
                provenance, config, getattr(args, "config", None), receipts
            ),
            "model_calls": 0,
            "data_cost": 0,
        },
        "errors": [{"source": r["source_id"], "code": r["error"] or r["status"]} for r in errors],
    }


def parser():
    p = argparse.ArgumentParser(prog="radar")
    p.add_argument("--data-dir", default=os.environ.get("RADAR_DATA_DIR", "var/radar"))
    p.add_argument("--config", default="config/sources.local.json")
    p.add_argument("--json", action="store_true", help="JSON is always used for machine-readable output")
    p.add_argument("--idempotency-key")
    p.add_argument("--run-id")
    p.add_argument("--ledger-dir", help="Shared model ledger path; defaults to data directory")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("capabilities")
    s = sub.add_parser("sources")
    s.add_argument("action", choices=["status"])
    c = sub.add_parser("collect")
    c.add_argument("--sources")
    c.add_argument("--max-requests", type=int, default=200)
    c.add_argument("--max-seconds", type=int, default=240)
    c.add_argument("--due", action="store_true")
    c.add_argument(
        "--data-origin",
        choices=ORIGINS,
        default="unknown",
        help="Operator-declared origin; unknown by default, never inferred from path or host",
    )
    c.add_argument(
        "--raw-cache", action="store_true", help="Opt in to bounded internal raw archive; never auto-prune"
    )
    i = sub.add_parser("items")
    i.add_argument("--source")
    i.add_argument("--limit", type=int, default=100)
    i.add_argument("--posts-only", action="store_true")
    changes = sub.add_parser("changes")
    changes.add_argument("--after", type=int, default=0)
    changes.add_argument("--limit", type=int, default=100)
    backup = sub.add_parser("backup")
    backup.add_argument("destination")
    restore = sub.add_parser("restore")
    restore.add_argument("snapshot")
    restore.add_argument("destination")
    verify = sub.add_parser("verify-snapshot")
    verify.add_argument("snapshot")
    events = sub.add_parser("events")
    events.add_argument("action", choices=["rebuild", "list", "show"])
    events.add_argument("--event-id")
    events.add_argument("--cutoff")
    events.add_argument("--since")
    events.add_argument("--limit", type=int, default=100)
    analysis = sub.add_parser("analyze")
    analysis.add_argument("--model-config", default="config/model.local.json")
    analysis.add_argument("--since")
    analysis.add_argument("--cutoff")
    analysis.add_argument("--event-ids")
    analysis.add_argument("--limit", type=int, default=4)
    digest = sub.add_parser("digest")
    digest.add_argument("action", choices=["build"])
    digest.add_argument("--since")
    digest.add_argument("--cutoff")
    digest.add_argument("--limit", type=int, default=8)
    question = sub.add_parser("ask")
    question.add_argument("question")
    question.add_argument("--since")
    question.add_argument("--cutoff")
    question.add_argument("--limit", type=int, default=5)
    feedback = sub.add_parser("feedback")
    feedback.add_argument("action", choices=["add"])
    feedback.add_argument("--target-id", required=True)
    feedback.add_argument(
        "--label", required=True, choices=["useful", "known", "too_broad", "duplicate", "follow"]
    )
    feedback.add_argument("--actor", default="operator")
    feedback.add_argument("--actor-type", choices=["operator", "reviewer", "user"], default="operator")
    feedback.add_argument("--interaction-receipt")
    usage = sub.add_parser("usage")
    usage.add_argument("--scope", choices=["run", "day", "month"], default="day")
    sub.add_parser("outbox")
    return p


def finalize_receipt(envelope, resources, data_dir, write_artifact=False):
    envelope.setdefault("finished_at", utcnow())
    envelope["resources"] = resources.finish()
    if data_dir.exists():
        envelope["resources"]["data_directory_bytes"] = disk_bytes(data_dir)
        envelope["resources"]["disk_measurement_basis"] = "before_receipt_persistence"
    if write_artifact:
        receipt_dir = data_dir / "receipts"
        receipt_dir.mkdir(exist_ok=True)
        path = receipt_dir / f"{envelope['run_id']}.json"
        envelope["artifacts"].append(str(path))
        atomic_write_json(path, envelope)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    run_id = args.run_id or uuid.uuid4().hex
    envelope = {
        "schema_version": 1,
        "run_id": run_id,
        "operation": args.command,
        "started_at": utcnow(),
        "status": "success",
        "data": {},
        "warnings": [],
        "errors": [],
        "usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "data_cost": 0},
        "artifacts": [],
    }
    data_dir = Path(args.data_dir).resolve()
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise ValueError("invalid_run_id")
        with ResourceSample() as resources:
            if args.command == "restore":
                envelope["data"] = restore_snapshot(Path(args.snapshot), Path(args.destination))
            elif args.command == "verify-snapshot":
                envelope["data"] = validate_snapshot(Path(args.snapshot))
            elif args.command == "capabilities":
                envelope["data"] = {
                    "version": __version__,
                    "schema_version": 1,
                    "operations": [
                        "collect",
                        "sources status",
                        "items",
                        "changes",
                        "backup",
                        "restore",
                        "verify-snapshot",
                        "events rebuild/list/show",
                        "analyze",
                        "digest build",
                        "ask",
                        "feedback add",
                        "usage",
                        "outbox",
                    ],
                    "adapters": list(ADAPTERS),
                    "agent_dependency": None,
                    "analysis": "claude_cli_no_tools",
                    "delivery": "not_configured",
                }
            else:
                assert_runtime_permission(data_dir, "collect" if args.command == "collect" else "read")
                data_dir.mkdir(parents=True, exist_ok=True)
                with portalocker.Lock(data_dir / "writer.lock", timeout=0), Store(data_dir) as db:
                    config = load_config(args.config) if args.command == "collect" else None
                    request = {k: v for k, v in vars(args).items() if k not in {"run_id", "idempotency_key"}}
                    request_hash = hashlib.sha256(json_text([request, config]).encode()).hexdigest()
                    receipt_key = args.idempotency_key or (
                        "collect-run:" + run_id if args.command == "collect" else None
                    )
                    old = db.receipt(receipt_key, request_hash) if receipt_key else None
                    if old:
                        print(json.dumps(old, ensure_ascii=False))
                        return {"success": 0, "partial": 2, "failed": 1, "skipped": 3}[old["status"]]
                    if args.command == "collect":
                        envelope.update(collect(db, args, config, run_id))
                    elif args.command == "sources":
                        envelope["data"] = {"coverage": db.source_status()}
                    elif args.command == "items":
                        envelope["data"] = {"items": db.items(args.limit, args.source, not args.posts_only)}
                    elif args.command == "changes":
                        envelope["data"] = db.changes(args.after, args.limit)
                    elif args.command == "backup":
                        envelope["data"] = db.backup(Path(args.destination))
                    elif args.command == "events":
                        if args.action == "rebuild":
                            envelope["data"] = rebuild(db)
                        else:
                            events = list_events(db, args.cutoff, args.since, args.limit, args.event_id)
                            if args.action == "show":
                                if not args.event_id or not events:
                                    raise ValueError("event_not_found")
                                events[0]["evidence"] = event_evidence(db, events[0])
                            envelope["data"] = {"events": events, "coverage": db.source_status()}
                    elif args.command == "analyze":
                        cfg = llm.load_config(args.model_config)
                        provider = llm.create_provider(cfg)
                        ledger_path = Path(args.ledger_dir).resolve() if args.ledger_dir else data_dir
                        if ledger_path == data_dir:
                            runner = llm.ModelRunner(db, provider, cfg, run_id)
                            result = editor.analyze(
                                db,
                                runner,
                                args.since,
                                args.cutoff,
                                args.event_ids.split(",") if args.event_ids else None,
                                args.limit,
                            )
                            envelope["usage"] = runner.usage("run")
                        else:
                            ledger_path.mkdir(parents=True, exist_ok=True)
                            with (
                                portalocker.Lock(ledger_path / "writer.lock", timeout=0),
                                Store(ledger_path) as ledger,
                            ):
                                runner = llm.ModelRunner(db, provider, cfg, run_id, ledger_db=ledger)
                                result = editor.analyze(
                                    db,
                                    runner,
                                    args.since,
                                    args.cutoff,
                                    args.event_ids.split(",") if args.event_ids else None,
                                    args.limit,
                                )
                                envelope["usage"] = runner.usage("run")
                        envelope["data"] = result
                        envelope["status"] = result.get("status", "success")
                    elif args.command == "digest":
                        envelope["data"] = editor.build_digest(
                            db, args.since, args.cutoff, args.limit, data_dir / "reports"
                        )
                    elif args.command == "ask":
                        envelope["data"] = editor.ask(db, args.question, args.since, args.cutoff, args.limit)
                    elif args.command == "feedback":
                        delivery.initialize(db)
                        envelope["data"] = delivery.add_feedback(
                            db,
                            args.target_id,
                            args.label,
                            args.actor,
                            args.actor_type,
                            args.interaction_receipt,
                        )
                    elif args.command == "outbox":
                        delivery.initialize(db)
                        envelope["data"] = {
                            "items": [
                                delivery.get(db, r[0])
                                for r in db.conn.execute(
                                    "SELECT delivery_key FROM outbox ORDER BY created_at DESC LIMIT 100"
                                )
                            ]
                        }
                    elif args.command == "usage":
                        if args.ledger_dir and Path(args.ledger_dir).resolve() != data_dir:
                            with Store(Path(args.ledger_dir)) as ledger:
                                envelope["data"] = llm.ModelRunner(
                                    db, None, llm.load_config(), run_id, ledger_db=ledger
                                ).usage(args.scope)
                        else:
                            runner = llm.ModelRunner(db, None, llm.load_config(), run_id)
                            envelope["data"] = runner.usage(args.scope)
                    finalize_receipt(envelope, resources, data_dir, args.command in {"collect", "backup"})
                    if receipt_key:
                        db.save_receipt(receipt_key, request_hash, envelope)
        if "resources" not in envelope:
            finalize_receipt(envelope, resources, data_dir)
    except portalocker.exceptions.LockException:
        envelope.update(status="failed", errors=[{"code": "writer_busy"}])
    except (ValueError, OSError, sqlite3.Error) as error:
        envelope.update(
            status="failed",
            errors=[{"code": str(error) if isinstance(error, ValueError) else type(error).__name__}],
        )
    envelope.setdefault("finished_at", utcnow())
    print(json.dumps(envelope, ensure_ascii=False))
    return {"success": 0, "partial": 2, "failed": 1, "skipped": 3}[envelope["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
