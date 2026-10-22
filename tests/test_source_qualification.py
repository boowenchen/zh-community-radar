"""Qualification fixtures are synthetic; they cannot certify a real source."""

import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "source_qualification", Path(__file__).parents[1] / "tools/source_qualification.py"
)
qualification_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification_tool)

CONFIG = {"sources": [{"id": "s", "enabled": True, "interval_minutes": 30}]}


def sample(count=20, *, spacing_minutes=30, completed=20, host="vm-one", egress="direct"):
    runs, operations = [], {}
    start = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(count):
        at = (start + timedelta(minutes=spacing_minutes * i)).isoformat()
        status = "ok" if i < completed else "blocked"
        receipt = {
            "source_id": "s",
            "observed_at": at,
            "host": host,
            "requested_egress": egress,
            "effective_egress": egress,
            "requests": 1,
            "complete": i < completed,
            "status": status,
            "error": None if i < completed else "http_403",
        }
        runs.append(
            {
                "run_id": str(i),
                "source_id": "s",
                "observed_at": at,
                "status": status,
                "complete": i < completed,
                "receipt": json.dumps(receipt),
            }
        )
        operations[str(i)] = {"operation": "collect", "due_only": True, "http_receipt_count": 1}
    return {"runs": runs, "operations": operations, "meta": {}, "item_counts": {"s": 10000}}


def report(snapshot, **kwargs):
    return qualification_tool.qualification(
        snapshot,
        CONFIG,
        data_origin=kwargs.pop("data_origin", "live"),
        selected_host="vm-one",
        selected_egress="direct",
        selected_host_kind="vm",
        **kwargs,
    )["sources"][0]


def test_twenty_spaced_runs_are_candidates_not_certification_or_reference_denominator():
    result = report(sample(completed=19))
    assert result["core"]["selected_cadence_candidate_count"] == 20
    assert result["core"]["candidate_completion_ratio"] == 0.95
    assert result["core"]["candidate_threshold_status"] == "pass"
    assert result["core"]["status"] == "pending" and not result["core"]["eligible"]
    assert result["core"]["eligible_live_runs"] == 0
    assert result["accuracy_check"]["verified_comparison_samples"] == 0
    assert result["coverage_check"]["reference_denominator"] is None
    assert result["coverage_check"]["retrieval_ratio"] is None


def test_failures_remain_in_completion_denominator():
    result = report(sample(completed=18))
    assert result["core"]["selected_cadence_candidate_count"] == 20
    assert result["core"]["candidate_completed"] == 18
    assert result["core"]["candidate_completion_ratio"] == 0.9
    assert result["core"]["candidate_threshold_status"] == "fail"
    assert result["partitions"][0]["status_counts"]["blocked"] == 2


@pytest.mark.parametrize("origin", ["replay", "fixture", "manual-probe", "unknown"])
def test_nonlive_origins_never_count_as_live(origin):
    result = report(sample(), data_origin=origin)
    assert result["core"]["selected_cadence_candidate_count"] == 0
    assert result["partitions"][0]["data_origin"] == origin


def test_recorded_replay_overrides_conflicting_live_declaration():
    snapshot = sample()
    snapshot["meta"]["data_origin"] = "legacy-replay"
    result = report(snapshot)
    assert result["partitions"][0]["data_origin"] == "replay"
    assert result["partitions"][0]["runs"][0]["origin_conflict"]
    assert result["core"]["selected_cadence_candidate_count"] == 0


def test_dense_polling_and_missing_operation_receipts_are_excluded():
    result = report(sample(spacing_minutes=1))
    assert result["core"]["selected_cadence_candidate_count"] == 1
    assert result["partitions"][0]["exclusion_counts"]["shorter_than_current_config_interval"] == 19
    snapshot = sample()
    snapshot["operations"] = {}
    assert report(snapshot)["core"]["selected_cadence_candidate_count"] == 0


def test_host_and_actual_egress_are_not_combined():
    snapshot = sample()
    for i, row in enumerate(snapshot["runs"]):
        receipt = json.loads(row["receipt"])
        if i >= 10:
            receipt["host"] = "local-collector"
        elif i >= 5:
            receipt["effective_egress"] = receipt["requested_egress"] = "pc-egress"
        row["receipt"] = json.dumps(receipt)
    result = report(snapshot)
    assert len(result["partitions"]) == 3
    assert result["core"]["selected_cadence_candidate_count"] == 5


def test_no_cloud_selection_cannot_promote_pc_data():
    result = qualification_tool.qualification(sample(host="local-collector"), CONFIG, data_origin="live")
    assert result["sources"][0]["core"]["selected_cadence_candidate_count"] == 0
    assert result["summary"]["core_eligible_sources"] == 0
    with pytest.raises(ValueError, match="explicit_valid_data_origin_required"):
        qualification_tool.qualification(sample(), CONFIG, data_origin=None)


def test_config_only_source_does_not_inflate_database_source_count():
    config = {"sources": CONFIG["sources"] + [{"id": "unobserved", "enabled": False, "interval_minutes": 60}]}
    result = qualification_tool.qualification(sample(), config, data_origin="fixture")
    assert result["summary"]["configured_sources"] == 2
    assert result["summary"]["database_sources"] == 1
    assert result["summary"]["configured_observed_union_sources"] == 2


def test_snapshot_reader_does_not_migrate_or_write_database(tmp_path):
    database = tmp_path / "source.sqlite3"
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE source_runs(run_id,source_id,observed_at,status,complete,receipt)")
        row = sample(1)["runs"][0]
        con.execute("INSERT INTO source_runs VALUES(?,?,?,?,?,?)", tuple(row.values()))
        con.execute("PRAGMA user_version=123")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    snapshot = qualification_tool.read_snapshot(database)
    assert len(snapshot["runs"]) == 1
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with sqlite3.connect(database) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 123
        assert con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 1


def test_cli_requires_origin_and_cannot_replace_inputs(tmp_path):
    database, config = tmp_path / "source.db", tmp_path / "sources.json"
    with pytest.raises(SystemExit) as error:
        qualification_tool.main(["--database", str(database), "--config", str(config), "--output", "x.json"])
    assert error.value.code == 2
    with pytest.raises(ValueError, match="output_must_not_overwrite_inputs"):
        qualification_tool.main(
            [
                "--database",
                str(database),
                "--config",
                str(config),
                "--output",
                str(database),
                "--data-origin",
                "fixture",
            ]
        )
