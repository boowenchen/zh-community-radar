"""Synthetic local provenance checks; no provider or upstream network calls."""

import copy
import json
from unittest.mock import patch

import pytest
from test_runtime import Runner
from test_source_qualification import qualification_tool, sample

from radar import cli, provenance
from radar.models import Item, Scan
from radar.runtime import RuntimeConfig, run_tick
from radar.store import Store

PROGRAM = {
    "package": "zh-community-radar",
    "version": "fixture",
    "source_basis": "package_python_files",
    "source_sha256": "a" * 64,
}
CONFIG = {
    "schema_version": 1,
    "host_role": "vm-one",
    "sources": [
        {
            "id": "s",
            "adapter": "feed",
            "family": "rss",
            "enabled": True,
            "data_cost": 0,
            "interval_minutes": 30,
            "url": "https://example.com/feed",
        }
    ],
}


def bound(config, path, row, origin="live", hostname="measured-machine"):
    with (
        patch.object(provenance, "utcnow", return_value=row["observed_at"]),
        patch.object(provenance.platform, "node", return_value=hostname),
        patch.object(provenance, "program_identity", return_value=PROGRAM),
    ):
        begin = provenance.begin_collection(
            config, path, run_id=row["run_id"], data_origin=origin, due_only=True
        )
        return provenance.finish_collection(begin, config, path, [json.loads(row["receipt"])])


def inspect(value, row):
    return provenance.inspect_source(
        value, run_id=row["run_id"], source_id="s", receipt=json.loads(row["receipt"])
    )


def attest_snapshot(tmp_path, *, count=20, completed=19):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    snapshot = sample(count=count, completed=completed)
    for row in snapshot["runs"]:
        snapshot["operations"][row["run_id"]]["collection_provenance"] = bound(CONFIG, path, row)
    return snapshot


def test_new_receipts_use_historical_cadence_and_count_without_promoting_core(tmp_path):
    snapshot = attest_snapshot(tmp_path)
    current = copy.deepcopy(CONFIG)
    current["sources"][0]["interval_minutes"] = 120
    result = qualification_tool.qualification(
        snapshot,
        current,
        data_origin="live",
        selected_host="vm-one",
        selected_egress="direct",
        selected_host_kind="vm",
        selected_actual_hostname="measured-machine",
    )["sources"][0]
    assert result["core"]["eligible_live_runs"] == 20
    assert result["core"]["provenance_completion_ratio"] == 0.95
    assert result["core"]["provenance_threshold_status"] == "pass"
    assert result["core"]["eligible"] is False
    assert result["accuracy_check"]["status"] == result["coverage_check"]["status"] == "pending"
    assert "historical_config_and_code_version_not_attested" not in result["core"]["reasons"]
    assert result["partitions"][0]["runs"][0]["interval_basis"] == "recorded_historical_config"


@pytest.mark.parametrize("origin", [None, "unknown", "live", "fixture", "replay", "manual-probe"])
def test_origin_default_is_unknown_and_explicit_declaration_is_named(tmp_path, origin):
    row = sample(1)["runs"][0]
    path = tmp_path / "source.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    result = bound(CONFIG, path, row, origin)
    assert result["data_origin"] == (origin or "unknown")
    expected = origin not in {None, "unknown"}
    assert inspect(result, row)["verified"] == expected
    assert result["attestation_basis"] == ("operator_declared" if expected else "not_declared")


@pytest.mark.parametrize("mutation", ["hash", "run", "source", "hostname", "time"])
def test_receipt_mismatch_and_modified_claims_fail_closed(tmp_path, mutation):
    snapshot = attest_snapshot(tmp_path, count=1)
    row = snapshot["runs"][0]
    value = copy.deepcopy(snapshot["operations"][row["run_id"]]["collection_provenance"])
    if mutation == "hash":
        value["source_config"]["sources"]["s"]["interval_minutes"] = 1
    elif mutation == "run":
        row["run_id"] = "different"
    elif mutation == "source":
        receipt = json.loads(row["receipt"])
        receipt["status"] = "different"
        row["receipt"] = json.dumps(receipt)
    elif mutation == "hostname":
        value["actual_hostname"] = "cloud-by-name-only"
    else:
        value["finished_at"] = "2000-01-01T00:00:00Z"
        value["binding_sha256"] = provenance.fingerprint(
            {k: v for k, v in value.items() if k != "binding_sha256"}
        )
    assert inspect(value, row)["verified"] is False


@pytest.mark.parametrize("mutation", ["program", "config-file", "hostname"])
def test_mid_collection_drift_is_recorded_and_not_qualified(tmp_path, monkeypatch, mutation):
    row = sample(1)["runs"][0]
    path = tmp_path / "source.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    monkeypatch.setattr(provenance, "program_identity", lambda: PROGRAM)
    monkeypatch.setattr(provenance, "utcnow", lambda: row["observed_at"])
    monkeypatch.setattr(provenance.platform, "node", lambda: "measured-machine")
    begin = provenance.begin_collection(CONFIG, path, run_id=row["run_id"], data_origin="live")
    if mutation == "program":
        monkeypatch.setattr(provenance, "program_identity", lambda: PROGRAM | {"source_sha256": "b" * 64})
    elif mutation == "config-file":
        path.write_text(json.dumps(CONFIG) + "\n", encoding="utf-8")
    else:
        monkeypatch.setattr(provenance.platform, "node", lambda: "other-machine")
    result = provenance.finish_collection(begin, CONFIG, path, [json.loads(row["receipt"])])
    assert not inspect(result, row)["verified"]


def test_legacy_and_different_physical_hosts_are_not_backfilled_or_pooled(tmp_path):
    snapshot = attest_snapshot(tmp_path)
    row = snapshot["runs"][0]
    snapshot["operations"][row["run_id"]].pop("collection_provenance")
    row = snapshot["runs"][1]
    path = tmp_path / "sources.json"
    snapshot["operations"][row["run_id"]]["collection_provenance"] = bound(
        CONFIG, path, row, hostname="other-machine"
    )
    result = qualification_tool.qualification(
        snapshot,
        CONFIG,
        data_origin="live",
        selected_host="vm-one",
        selected_egress="direct",
        selected_host_kind="vm",
        selected_actual_hostname="measured-machine",
    )["sources"][0]
    assert len(result["partitions"]) == 3
    assert result["core"]["eligible_live_runs"] == 18
    assert result["core"]["provenance_threshold_status"] == "insufficient"


@pytest.mark.parametrize(
    "mutation", ["missing-physical-selection", "meta-replay", "due-mode", "declared-fixture"]
)
def test_traceable_receipts_do_not_override_scope_origin_or_operation_conflicts(tmp_path, mutation):
    snapshot = attest_snapshot(tmp_path)
    actual, declared = "measured-machine", "live"
    if mutation == "missing-physical-selection":
        actual = None
    elif mutation == "meta-replay":
        snapshot["meta"]["data_origin"] = "legacy-replay"
    elif mutation == "declared-fixture":
        declared = "fixture"
    else:
        for operation in snapshot["operations"].values():
            operation["due_only"] = False
    result = qualification_tool.qualification(
        snapshot,
        CONFIG,
        data_origin=declared,
        selected_host="vm-one",
        selected_egress="direct",
        selected_host_kind="vm",
        selected_actual_hostname=actual,
    )["sources"][0]
    assert result["core"]["eligible_live_runs"] == 0
    assert result["core"]["eligible"] is False


def test_snapshot_refuses_ambiguous_operation_receipts_without_writing(tmp_path):
    import sqlite3

    path = tmp_path / "conflict.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE source_runs(run_id,source_id,observed_at,status,complete,receipt)")
        conn.execute("CREATE TABLE receipts(receipt)")
        for due in (True, False):
            conn.execute(
                "INSERT INTO receipts VALUES(?)",
                (json.dumps({"run_id": "x", "operation": "collect", "data": {"due_only": due, "http": []}}),),
            )
    snapshot = qualification_tool.read_snapshot(path)
    assert snapshot["operations"]["x"]["ambiguous_operation_receipts"]


def test_timeout_without_http_response_stays_in_completion_denominator(tmp_path):
    snapshot = attest_snapshot(tmp_path, count=22, completed=20)
    for row in snapshot["runs"][20:]:
        receipt = json.loads(row["receipt"])
        receipt.update(status="network_error", error="ReadTimeout")
        row.update(status="network_error", receipt=json.dumps(receipt))
        operation = snapshot["operations"][row["run_id"]]
        operation["http_receipt_count"] = 0
        operation["collection_provenance"] = bound(CONFIG, tmp_path / "sources.json", row)
    result = qualification_tool.qualification(
        snapshot,
        CONFIG,
        data_origin="live",
        selected_host="vm-one",
        selected_egress="direct",
        selected_host_kind="vm",
        selected_actual_hostname="measured-machine",
    )["sources"][0]
    assert result["core"]["eligible_live_runs"] == 22
    assert result["core"]["provenance_completed"] == 20
    assert result["core"]["provenance_completion_ratio"] == 20 / 22
    assert result["core"]["provenance_threshold_status"] == "fail"
    assert result["partitions"][0]["runs"][-1]["response_evidence"] == "recorded_transport_failure"
    # Missing HTTP evidence must not be promoted to a successful attempt.
    snapshot["operations"]["0"]["http_receipt_count"] = 0
    result = qualification_tool.qualification(
        snapshot,
        CONFIG,
        data_origin="live",
        selected_host="vm-one",
        selected_egress="direct",
        selected_host_kind="vm",
        selected_actual_hostname="measured-machine",
    )["sources"][0]
    assert result["core"]["eligible_live_runs"] == 21
    assert result["core"]["provenance_completed"] == 19


@pytest.mark.parametrize("origin", [None, "live"])
def test_cli_persists_joinable_receipt_without_idempotency_key(tmp_path, monkeypatch, capsys, origin):
    class FakeFetcher:
        def __init__(self, *args):
            self.requests, self.receipts = 0, []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def adapter(source, fetcher, checkpoint, known):
        fetcher.requests += 1
        fetcher.receipts.append({"status": 200, "synthetic": True})
        return Scan(
            "s", [Item("s", "fixture", "https://example.com/fixture", "Fixture", "Synthetic")], complete=True
        )

    path = tmp_path / "sources.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    monkeypatch.setattr(cli, "Fetcher", FakeFetcher)
    monkeypatch.setitem(cli.ADAPTERS, "feed", adapter)
    monkeypatch.setattr(provenance, "program_identity", lambda: PROGRAM)
    monkeypatch.setattr(provenance.platform, "node", lambda: "measured-machine")
    args = [
        "--config",
        str(path),
        "--data-dir",
        str(tmp_path / "data"),
        "--run-id",
        "synthetic",
        "collect",
        "--due",
    ]
    if origin:
        args += ["--data-origin", origin]
    assert cli.main(args) == 0
    envelope = json.loads(capsys.readouterr().out)
    snapshot = qualification_tool.read_snapshot(tmp_path / "data/radar.sqlite3")
    assert (
        snapshot["operations"]["synthetic"]["collection_provenance"]
        == envelope["data"]["collection_provenance"]
    )
    result = inspect(snapshot["operations"]["synthetic"]["collection_provenance"], snapshot["runs"][0])
    assert result["verified"] == bool(origin)
    receipt_file = tmp_path / "data/receipts/synthetic.json"
    previous_bytes = receipt_file.read_bytes()
    assert cli.main(args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay == envelope
    assert receipt_file.read_bytes() == previous_bytes
    assert cli.main(args + ["--max-requests", "1"]) == 1
    conflict = json.loads(capsys.readouterr().out)
    assert conflict["errors"] == [{"code": "idempotency_conflict"}]
    assert receipt_file.read_bytes() == previous_bytes
    with Store(tmp_path / "data") as db:
        assert db.conn.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1


@pytest.mark.parametrize("origin", ["unknown", "live", "fixture"])
def test_runtime_passes_only_explicit_origin_to_collect(tmp_path, origin):
    seen, runner = [], Runner()

    def run(stage, args, **kwargs):
        if stage == "collect":
            seen.append(args)
        return runner(stage, args, **kwargs)

    cfg = RuntimeConfig(tmp_path, data_origin=origin)
    run_tick(cfg, runner=run, now="2026-10-22T00:00:00Z")
    assert seen[0][seen[0].index("--data-origin") + 1] == origin
    assert RuntimeConfig(tmp_path).data_origin == "unknown"
    with pytest.raises(ValueError, match="runtime_data_origin_invalid"):
        RuntimeConfig(tmp_path, data_origin="cloud-guessed-live").validate()
