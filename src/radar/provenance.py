"""Local collection evidence; origin is declared by an operator, never inferred.

Hashes bind recorded bytes and fields, not a trusted execution environment. An OS
hostname identifies the collector's observation, not a cloud provider or VM role.
Only explicitly supplied nonsecret source configuration files are inspected.
"""

import copy
import hashlib
import json
import math
import platform
import re
from datetime import datetime
from pathlib import Path

from .models import json_text, utcnow
from .store import program_identity

ORIGINS = ("live", "replay", "fixture", "manual-probe", "unknown")
HASH = re.compile(r"[0-9a-f]{64}")


def fingerprint(value):
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


def scan_fingerprint(receipt):
    # These two fields are added only to the operation view after save_scan.
    return fingerprint({k: v for k, v in receipt.items() if k not in {"family", "raw_cache"}})


def _config_file(config, path):
    if path is None:
        return {"status": "unavailable", "reason": "source_config_path_not_supplied"}
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
            raise ValueError("invalid_source_config_file")
        raw = path.read_bytes()
        if json.loads(raw) != config:
            raise ValueError("loaded_config_differs_from_file")
        return {"status": "recorded", "file_sha256": hashlib.sha256(raw).hexdigest()}
    except (OSError, TypeError, ValueError):
        return {"status": "unavailable", "reason": "source_config_file_not_bound_to_loaded_config"}


def begin_collection(config, config_path, *, run_id, data_origin=None, due_only=False):
    if data_origin is not None and data_origin not in ORIGINS:
        raise ValueError("collection_data_origin_invalid")
    declared = data_origin is not None and data_origin != "unknown"
    sources = {}
    for source in config["sources"]:
        sources[source["id"]] = {
            "enabled": source.get("enabled"),
            "adapter": source.get("adapter"),
            "interval_minutes": source.get("interval_minutes"),
            "effective_source_sha256": fingerprint(source),
        }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "due_only": due_only is True,
        "started_at": utcnow(),
        "data_origin": data_origin or "unknown",
        "attestation_basis": "operator_declared" if declared else "not_declared",
        "actual_hostname": platform.node(),
        "hostname_basis": "executing_os_platform_node_not_cloud_identity",
        "declared_host_role": config.get("host_role"),
        "program": program_identity(),
        "source_config": _config_file(config, config_path)
        | {"effective_config_sha256": fingerprint(config), "sources": sources},
    }


def finish_collection(initial, config, config_path, coverage):
    result = copy.deepcopy(initial)
    result["finished_at"] = utcnow()
    after_program = program_identity()
    after_file = _config_file(config, config_path)
    result["checks"] = {
        "program_unchanged": after_program == result["program"],
        "loaded_config_unchanged": fingerprint(config) == result["source_config"]["effective_config_sha256"],
        "config_file_unchanged": after_file.get("status") == "recorded"
        and after_file.get("file_sha256") == result["source_config"].get("file_sha256"),
        "actual_hostname_unchanged": platform.node() == result["actual_hostname"],
    }
    result["source_receipts"] = {row["source_id"]: scan_fingerprint(row) for row in coverage}
    result["binding_sha256"] = fingerprint(result)
    return result


def inspect_source(provenance, *, run_id, source_id, receipt):
    """Read-only integrity checks for a persisted operation/source-run association."""
    absent = {"verified": False, "reasons": ["collection_provenance_missing"]}
    if not isinstance(provenance, dict):
        return absent
    reasons = []
    if provenance.get("schema_version") != 1 or provenance.get("run_id") != run_id:
        reasons.append("collection_provenance_identity_mismatch")
    if provenance.get("binding_sha256") != fingerprint(
        {k: v for k, v in provenance.items() if k != "binding_sha256"}
    ):
        reasons.append("collection_provenance_hash_mismatch")
    checks = provenance.get("checks")
    required_checks = {
        "program_unchanged",
        "loaded_config_unchanged",
        "config_file_unchanged",
        "actual_hostname_unchanged",
    }
    if not isinstance(checks, dict) or any(checks.get(k) is not True for k in required_checks):
        reasons.append("collection_identity_changed_or_unavailable")
    program = provenance.get("program")
    program = program if isinstance(program, dict) else {}
    config = provenance.get("source_config")
    config = config if isinstance(config, dict) else {}
    sources = config.get("sources")
    source = sources.get(source_id) if isinstance(sources, dict) else None
    source = source if isinstance(source, dict) else {}
    if (
        program.get("package") != "zh-community-radar"
        or not isinstance(program.get("version"), str)
        or program.get("source_basis") != "package_python_files"
        or not HASH.fullmatch(str(program.get("source_sha256", "")))
        or config.get("status") != "recorded"
        or not HASH.fullmatch(str(config.get("file_sha256", "")))
        or not HASH.fullmatch(str(config.get("effective_config_sha256", "")))
        or not HASH.fullmatch(str(source.get("effective_source_sha256", "")))
    ):
        reasons.append("collection_program_or_config_identity_unavailable")
    interval = source.get("interval_minutes")
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval <= 0
        or source.get("enabled") is not True
    ):
        reasons.append("historical_source_cadence_unavailable")
        interval = None
    bindings = provenance.get("source_receipts")
    if not isinstance(bindings, dict) or bindings.get(source_id) != scan_fingerprint(receipt):
        reasons.append("collection_source_receipt_binding_mismatch")
    host = provenance.get("actual_hostname")
    if not isinstance(host, str) or not host or len(host) > 255:
        reasons.append("actual_hostname_unavailable")
        host = None
    origin = provenance.get("data_origin")
    if origin not in ORIGINS or provenance.get("attestation_basis") != "operator_declared":
        reasons.append("collection_origin_not_explicitly_declared")
    if provenance.get("hostname_basis") != "executing_os_platform_node_not_cloud_identity":
        reasons.append("hostname_measurement_basis_unknown")
    if provenance.get("declared_host_role") is not None and provenance.get(
        "declared_host_role"
    ) != receipt.get("host"):
        reasons.append("declared_host_role_mismatch")
    try:
        start, observed, end = (
            datetime.fromisoformat(value)
            for value in (
                provenance.get("started_at"),
                receipt.get("observed_at"),
                provenance.get("finished_at"),
            )
        )
        if not all(value.tzinfo for value in (start, observed, end)) or not start <= observed <= end:
            reasons.append("source_observation_outside_collection_window")
    except (TypeError, ValueError):
        reasons.append("collection_time_window_invalid")
    return {
        "verified": not reasons,
        "reasons": reasons,
        "actual_hostname": host,
        "data_origin": origin,
        "attestation_basis": provenance.get("attestation_basis"),
        "historical_interval_minutes": interval,
        "program_source_sha256": program.get("source_sha256"),
        "source_config_file_sha256": config.get("file_sha256"),
        "effective_source_sha256": source.get("effective_source_sha256"),
        "binding_sha256": provenance.get("binding_sha256"),
    }
