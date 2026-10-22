"""Read-only source evidence inventory. Timestamp candidates are not core certification."""

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from radar.provenance import inspect_source

ORIGINS = ("live", "replay", "fixture", "manual-probe", "unknown")
SUCCESS = {"ok", "no_new"}
MIN_RUNS = 20
MIN_COMPARISONS = 30
REQUIRED_RATIO = 0.95


def instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_snapshot(database):
    """Do not instantiate Store: that constructor can migrate/create tables."""
    database = Path(database).resolve(strict=True)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "source_runs" not in tables:
            raise ValueError("source_runs_table_missing")
        runs = [
            dict(row)
            for row in con.execute("SELECT * FROM source_runs ORDER BY observed_at,run_id,source_id")
        ]
        meta = (
            dict(con.execute("SELECT key,value FROM meta WHERE key='data_origin'"))
            if "meta" in tables
            else {}
        )
        item_counts = (
            dict(con.execute("SELECT source_id,count(*) FROM items GROUP BY source_id"))
            if "items" in tables
            else {}
        )
        operations = {}
        if "receipts" in tables:
            for row in con.execute("SELECT receipt FROM receipts"):
                try:
                    receipt = json.loads(row[0])
                    if not isinstance(receipt, dict) or not receipt.get("run_id"):
                        continue
                    data = receipt.get("data", {})
                    if not isinstance(data, dict):
                        continue
                    http = data.get("http", [])
                    operation = {
                        "operation": receipt.get("operation"),
                        "due_only": data.get("due_only"),
                        "http_receipt_count": len(http) if isinstance(http, list) else 0,
                        "collection_provenance": data.get("collection_provenance"),
                    }
                    old = operations.get(receipt["run_id"])
                    if old is not None and old != operation:
                        operation["ambiguous_operation_receipts"] = True
                    operations[receipt["run_id"]] = operation
                except (ValueError, TypeError):
                    continue
        return {"runs": runs, "meta": meta, "item_counts": item_counts, "operations": operations}


def classify_origin(declared, database_origin, receipt):
    """A conflicting recorded origin can disqualify; absence cannot prove live."""
    aliases = {"legacy-replay": "replay", "manual_probe": "manual-probe"}
    recorded = receipt.get("data_origin") or database_origin
    if recorded and not isinstance(recorded, str):
        recorded = "unknown"
    recorded = aliases.get(recorded, recorded)
    capabilities = receipt.get("capabilities", {})
    if isinstance(capabilities, dict) and capabilities.get("historical_summary"):
        recorded = "replay"
    if isinstance(capabilities, dict) and capabilities.get("live") is False and not recorded:
        recorded = "unknown"
    if recorded and recorded not in ORIGINS:
        recorded = "unknown"
    return recorded or declared, bool(recorded and recorded != declared), recorded


def qualification(
    snapshot,
    config,
    *,
    data_origin,
    selected_host=None,
    selected_egress=None,
    selected_host_kind="unknown",
    selected_actual_hostname=None,
    as_of=None,
):
    if data_origin not in ORIGINS:
        raise ValueError("explicit_valid_data_origin_required")
    as_of = as_of or datetime.now(UTC)
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("timezone_required")
    configs = {source["id"]: source for source in config["sources"]}
    if len(configs) != len(config["sources"]):
        raise ValueError("duplicate_source_config")
    by_source = defaultdict(list)
    for row in snapshot["runs"]:
        by_source[row["source_id"]].append(row)
    reports = []
    for source_id in sorted(set(configs) | set(by_source)):
        cfg = configs.get(source_id, {})
        interval = cfg.get("interval_minutes")
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval <= 0
        ):
            interval = None
        partitions = defaultdict(list)
        for row in by_source.get(source_id, []):
            try:
                receipt = json.loads(row["receipt"])
                if not isinstance(receipt, dict):
                    raise TypeError("not_object")
            except (ValueError, TypeError):
                receipt = {}
            host, egress = receipt.get("host"), receipt.get("effective_egress")
            host = host if isinstance(host, str) else None
            egress = egress if isinstance(egress, str) else None
            operation = snapshot["operations"].get(row["run_id"], {})
            provenance = inspect_source(
                operation.get("collection_provenance"),
                run_id=row["run_id"],
                source_id=source_id,
                receipt=receipt,
            )
            recorded_receipt = dict(receipt)
            if not recorded_receipt.get("data_origin") and provenance.get("data_origin"):
                recorded_receipt["data_origin"] = provenance["data_origin"]
            origin, conflict, recorded = classify_origin(
                data_origin, snapshot["meta"].get("data_origin"), recorded_receipt
            )
            database_origin = snapshot["meta"].get("data_origin")
            if database_origin and database_origin not in {origin, "legacy-" + str(origin)}:
                conflict = True
            historical_interval = (
                provenance.get("historical_interval_minutes") if provenance["verified"] else None
            )
            effective_interval = historical_interval or interval
            actual_hostname = provenance.get("actual_hostname") or "unknown"
            reasons = []
            if operation.get("ambiguous_operation_receipts"):
                reasons.append("ambiguous_operation_receipts")
            raw_provenance = operation.get("collection_provenance")
            if isinstance(raw_provenance, dict) and raw_provenance.get("due_only") != operation.get(
                "due_only"
            ):
                reasons.append("collection_due_mode_binding_mismatch")
            observed = instant(row.get("observed_at"))
            if not observed or observed > as_of:
                reasons.append("invalid_or_future_observation_time")
            if not host or not egress:
                reasons.append("missing_host_or_effective_egress")
            if origin != "live" or conflict:
                reasons.append("nonlive_or_conflicting_origin")
            if not effective_interval:
                reasons.append("current_interval_unknown")
            if operation.get("operation") != "collect" or operation.get("due_only") is not True:
                reasons.append("normal_cadence_execution_not_recorded")
            requests = receipt.get("requests")
            if isinstance(requests, bool) or not isinstance(requests, int) or requests < 1:
                reasons.append("no_upstream_request_recorded")
            transport_failure = (
                provenance["verified"]
                and row.get("status") == "network_error"
                and isinstance(receipt.get("error"), str)
                and bool(receipt["error"])
            )
            if not operation.get("http_receipt_count") and not transport_failure:
                reasons.append("no_operation_http_receipts")
            if receipt.get("source_id") != source_id or receipt.get("status") != row.get("status"):
                reasons.append("source_receipt_mismatch")
            if bool(receipt.get("complete")) != bool(row.get("complete")):
                reasons.append("completion_receipt_mismatch")
            if instant(receipt.get("observed_at")) != observed:
                reasons.append("observation_receipt_mismatch")
            requested = receipt.get("requested_egress")
            if requested != egress:
                reasons.append("requested_effective_egress_mismatch")
            partitions[(host or "unknown", egress or "unknown", origin, actual_hostname)].append(
                {
                    "run_id": row["run_id"],
                    "observed_at": row["observed_at"],
                    "status": row["status"],
                    "complete": bool(row["complete"]),
                    "requests": requests,
                    "error": receipt.get("error"),
                    "warnings": receipt.get("warnings", []),
                    "recorded_origin": recorded,
                    "origin_conflict": conflict,
                    "candidate_exclusions": reasons,
                    "provenance": provenance,
                    "interval_minutes_used": effective_interval,
                    "interval_basis": "recorded_historical_config"
                    if historical_interval
                    else "current_config_only",
                    "response_evidence": "http_response"
                    if operation.get("http_receipt_count")
                    else "recorded_transport_failure"
                    if transport_failure
                    else "missing",
                }
            )
        partition_reports = []
        selected_candidates = []
        selected_attested = []
        for (host, egress, origin, actual_hostname), rows in sorted(partitions.items()):
            rows.sort(
                key=lambda row: (
                    instant(row["observed_at"]) or datetime.min.replace(tzinfo=UTC),
                    row["run_id"],
                )
            )
            last_attempt = None
            for row in rows:
                observed = instant(row["observed_at"])
                # Compare every valid same-origin attempt, including failures/too-fast
                # attempts. Never let a failed scan disappear from cadence accounting.
                if observed and observed <= as_of:
                    if (
                        row["interval_minutes_used"]
                        and last_attempt
                        and (observed - last_attempt).total_seconds() < row["interval_minutes_used"] * 60
                    ):
                        row["candidate_exclusions"].append(
                            "shorter_than_historical_config_interval"
                            if row["interval_basis"] == "recorded_historical_config"
                            else "shorter_than_current_config_interval"
                        )
                    last_attempt = observed
            candidates = [row for row in rows if not row["candidate_exclusions"]]
            completed = sum(row["complete"] and row["status"] in SUCCESS for row in candidates)
            matching = host == selected_host and egress == selected_egress and origin == "live"
            attested = [row for row in candidates if row["provenance"]["verified"]]
            if matching:
                selected_candidates.extend(candidates)
                if actual_hostname == selected_actual_hostname:
                    selected_attested.extend(attested)
            partition_reports.append(
                {
                    "host": host,
                    "effective_egress": egress,
                    "data_origin": origin,
                    "actual_hostname": actual_hostname,
                    "raw_runs": len(rows),
                    "status_counts": dict(Counter(row["status"] for row in rows)),
                    "cadence_candidate_count": len(candidates),
                    "provenance_verified_candidate_count": len(attested),
                    "completed_candidate_count": completed,
                    "completion_ratio": completed / len(candidates) if candidates else None,
                    "selected_scope_matches": matching,
                    "exclusion_counts": dict(
                        Counter(reason for row in rows for reason in row["candidate_exclusions"])
                    ),
                    "runs": rows,
                }
            )
        scope_selected = bool(
            selected_host and selected_egress and selected_host_kind in {"vm", "managed-host"}
        )
        candidates = selected_candidates if scope_selected and cfg.get("enabled") else []
        attested = selected_attested if scope_selected and cfg.get("enabled") else []
        attested_completed = sum(row["complete"] and row["status"] in SUCCESS for row in attested)
        attested_ratio = attested_completed / len(attested) if attested else None
        completed = sum(row["complete"] and row["status"] in SUCCESS for row in candidates)
        ratio = completed / len(candidates) if candidates else None
        reasons = [
            "meaningful_collection_definition_not_fully_attested",
            "accuracy_check_pending",
            "coverage_check_pending",
        ]
        if not selected_actual_hostname:
            reasons.append("actual_hostname_scope_not_selected")
        if not attested:
            reasons.append("historical_config_and_code_version_not_attested")
        if not scope_selected:
            reasons.append("selected_cloud_host_and_actual_egress_not_supplied")
        if not cfg.get("enabled"):
            reasons.append("source_disabled_or_missing_current_config")
        if len(candidates) < MIN_RUNS:
            reasons.append("fewer_than_20_normal_cadence_candidates")
        failed_threshold = ratio is not None and len(candidates) >= MIN_RUNS and ratio < REQUIRED_RATIO
        if failed_threshold:
            reasons.append("candidate_completion_ratio_below_95_percent")
        reports.append(
            {
                "source_id": source_id,
                "enabled": bool(cfg.get("enabled")),
                "configured_tier": cfg.get("tier"),
                "interval_minutes": interval,
                "stored_item_count_not_a_reference_denominator": snapshot["item_counts"].get(source_id, 0),
                "partitions": partition_reports,
                "core": {
                    "status": "pending",
                    "eligible": False,
                    "candidate_threshold_status": (
                        "insufficient"
                        if len(candidates) < MIN_RUNS
                        else "fail"
                        if failed_threshold
                        else "pass"
                    ),
                    "eligible_live_runs": len(attested),
                    "eligible_live_runs_basis": "recorded_hashes_historical_cadence_operator_declared_live_selected_actual_hostname",
                    "provenance_completed": attested_completed,
                    "provenance_completion_ratio": attested_ratio,
                    "provenance_threshold_status": (
                        "insufficient"
                        if len(attested) < MIN_RUNS
                        else "pass"
                        if attested_ratio >= REQUIRED_RATIO
                        else "fail"
                    ),
                    "missing_provenance_runs": max(0, MIN_RUNS - len(attested)),
                    "selected_cadence_candidate_count": len(candidates),
                    "candidate_completed": completed,
                    "candidate_completion_ratio": ratio,
                    "minimum_live_runs": MIN_RUNS,
                    "missing_candidate_runs": max(0, MIN_RUNS - len(candidates)),
                    "required_completion_ratio": REQUIRED_RATIO,
                    "reasons": reasons,
                },
                "accuracy_check": {
                    "status": "pending",
                    "verified_comparison_samples": 0,
                    "minimum_samples": MIN_COMPARISONS,
                    "missing_samples": MIN_COMPARISONS,
                    "body_time_accuracy": None,
                    "required_accuracy": REQUIRED_RATIO,
                    "major_errors": None,
                    "reason": "source_runs_do_not_contain_machine_and_independent_original_comparisons",
                    "required": [
                        "selected_host_actual_egress",
                        "at_least_30_or_all_and_mark_insufficient",
                        "ID_URL_allowed_author_key_numbers_no_major_error",
                        "body_and_time_accuracy_95_percent",
                        "unknown_times_explicit",
                        "frozen_reference_locators_and_independent_review_result",
                    ],
                },
                "coverage_check": {
                    "status": "pending",
                    "comparable_samples": 0,
                    "minimum_samples": MIN_COMPARISONS,
                    "reference_denominator": None,
                    "retrieved_in_reference": None,
                    "retrieval_ratio": None,
                    "required_ratio": REQUIRED_RATIO,
                    "reason": "no_fixed_same_permission_reference_window_in_source_runs",
                    "required": [
                        "fixed_reference_window_and_matching_visibility",
                        "configured_scope_denominator",
                        "at_least_30_comparable_items",
                        "pagination_truncation_deletion_invisibility_classified",
                        "reference_snapshot_and_diff",
                        "no_legacy_database_as_current_denominator",
                    ],
                },
            }
        )
    return {
        "schema_version": 1,
        "generated_at": as_of.astimezone(UTC).isoformat(),
        "data_origin_declaration": data_origin,
        "provenance_automatically_proven": False,
        "database_recorded_origin": snapshot["meta"].get("data_origin"),
        "selected_scope": {
            "host": selected_host,
            "effective_egress": selected_egress,
            "host_kind": selected_host_kind,
            "actual_hostname": selected_actual_hostname,
            "host_kind_basis": "operator_selected_not_inferred_from_hostname",
        },
        "summary": {
            "configured_sources": len(configs),
            "database_sources": len(by_source),
            "configured_observed_union_sources": len(reports),
            "source_runs": len(snapshot["runs"]),
            "core_eligible_sources": 0,
            "candidate_threshold_fail_sources": sum(
                r["core"]["candidate_threshold_status"] == "fail" for r in reports
            ),
            "core_pending_sources": sum(r["core"]["status"] == "pending" for r in reports),
        },
        "limitations": [
            "Read-only inventory, not an automatic promotion tool.",
            "New bound receipts use recorded historical intervals; legacy records only use current config as a candidate filter.",
            "Hash/OS identity observations are local records, not cryptographic remote attestation; live origin remains operator-declared.",
            "No comparison results are inferred from stored item counts or HTTP success.",
            "Source evidence and independent reviewer acceptance require a separate reviewed artifact.",
            "Missing/partial/blocked attempts remain visible; no dropped-failure success denominator.",
        ],
        "sources": reports,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--data-origin",
        required=True,
        choices=ORIGINS,
        help="Operator declaration only; not automatic provenance proof",
    )
    parser.add_argument("--selected-host")
    parser.add_argument("--selected-egress")
    parser.add_argument("--selected-actual-hostname", help="Exact OS hostname measured by the collector")
    parser.add_argument(
        "--selected-host-kind", choices=("vm", "managed-host", "pc", "unknown"), default="unknown"
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    database, config_path, output = (Path(x).resolve() for x in (args.database, args.config, args.output))
    if output in {database, config_path} or str(output).startswith(str(database) + "-"):
        raise ValueError("output_must_not_overwrite_inputs_or_sqlite_sidecars")
    config_bytes = config_path.read_bytes()
    snapshot = read_snapshot(database)
    result = qualification(
        snapshot,
        json.loads(config_bytes),
        data_origin=args.data_origin,
        selected_host=args.selected_host,
        selected_egress=args.selected_egress,
        selected_actual_hostname=args.selected_actual_hostname,
        selected_host_kind=args.selected_host_kind,
    )
    result["evidence"] = {
        "database": str(database),
        "sqlite_readonly_uri": True,
        "query_only": True,
        "read_transaction": True,
        "source_runs_snapshot_sha256": digest(snapshot["runs"]),
        "config": str(config_path),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "specification": "core-source qualification gate",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **result["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
