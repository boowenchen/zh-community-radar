"""Preview never turns age or an inventory assertion into deletion permission."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from radar.retention import plan_retention

NOW = "2026-10-22T00:00:00Z"


def record(path="raw/body.json", observed="2026-09-01T00:00:00Z", kind="post"):
    return {
        "path": path,
        "artifact_kind": "http_snapshot",
        "http_status": 200,
        "parse_status": "success",
        "content_kind": kind,
        "observed_at": observed,
    }


def test_expired_successful_raw_is_only_unverified_candidate_and_nothing_changes(tmp_path):
    raw = tmp_path / "raw/body.json"
    raw.parent.mkdir()
    raw.write_bytes(b"fixture source body")
    before = hashlib.sha256(raw.read_bytes()).hexdigest()
    result = plan_retention(tmp_path, [record()], now=NOW)
    assert result["mode"] == "preview_only"
    assert result["archive_implemented"] is result["deletion_implemented"] is False
    assert result["actions"] == []
    assert result["entries"][0]["classification"] == "expired_unverified_candidate"
    assert "reference_closure_unavailable" in result["blockers"]
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == before
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == [
        "raw",
        "raw/body.json",
    ]


@pytest.mark.parametrize("content_kind,days", [("post", 30), ("comment", 14), ("mixed", 14)])
def test_content_policy_uses_explicit_type_and_observation_not_mtime(tmp_path, content_kind, days):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/body.json").write_bytes(b"created just now")
    result = plan_retention(tmp_path, [record(kind=content_kind)], now=NOW)
    assert result["entries"][0]["policy_days"] == days
    assert result["entries"][0]["classification"] == "expired_unverified_candidate"


@pytest.mark.parametrize(
    "path",
    [
        "radar.sqlite3",
        "raw/report.db",
        "reports/daily.json",
        "validation/raw.json",
        "holdout/raw.json",
        "raw/frozen/a.json",
    ],
)
def test_evidence_and_frozen_paths_are_never_candidates(tmp_path, path):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"keep")
    result = plan_retention(tmp_path, [record(path)], now=NOW)
    assert result["entries"][0]["classification"] == "protected"
    assert result["actions"] == [] and target.read_bytes() == b"keep"


def test_validation_root_remains_protected_even_if_relative_path_looks_like_raw(tmp_path):
    folder = tmp_path / "validation/data"
    (folder / "raw").mkdir(parents=True)
    (folder / "raw/body.json").write_bytes(b"keep")
    result = plan_retention(folder, [record()], now=NOW)
    assert result["entries"][0]["classification"] == "protected"


@pytest.mark.parametrize("path", ["../outside", "/outside", "C:/outside", "raw/body.json:stream"])
def test_path_escape_is_rejected_without_inspecting_target(tmp_path, path):
    result = plan_retention(tmp_path, [record(path)], now=NOW)
    assert result["entries"][0]["reason"] == "unsafe_relative_path"
    assert result["actions"] == []


def test_unknown_log_failure_and_recent_snapshot_are_not_expired_candidates(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/body.json").write_bytes(b"keep")
    records = [
        record() | {"artifact_kind": "log"},
        record() | {"http_status": 403},
        record() | {"parse_status": "unknown"},
        record(kind="unknown"),
        record(observed="2026-10-21T00:00:00Z"),
    ]
    result = plan_retention(tmp_path, records, now=NOW)
    assert all(row["classification"] != "expired_unverified_candidate" for row in result["entries"])
    assert result["actions"] == []


def test_symlink_is_not_followed(tmp_path):
    (tmp_path / "raw").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    try:
        (tmp_path / "raw/body.json").symlink_to(outside)
    except OSError:
        pytest.skip("Host cannot create a symlink fixture")
    result = plan_retention(tmp_path, [record()], now=NOW)
    assert result["entries"][0]["reason"] == "symlink_or_reparse_point"


def test_inventory_is_bounded_and_missing_catalog_is_explicit(tmp_path):
    assert plan_retention(tmp_path, [], now=NOW)["inventory_status"] == "not_supplied_or_empty"
    with pytest.raises(ValueError, match="retention_inventory_invalid"):
        plan_retention(tmp_path, [record()] * 1001, now=NOW)


def test_intermediate_windows_reparse_point_is_rejected_without_following(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "body.json").write_bytes(b"keep")
    original = Path.lstat

    def reparse_lstat(path):
        info = original(path)
        if path == raw:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400, st_size=info.st_size)
        return info

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    result = plan_retention(tmp_path, [record()], now=NOW)
    assert result["entries"][0]["reason"] == "symlink_or_reparse_point"
    assert result["actions"] == []


@pytest.mark.parametrize(
    "observed,reason",
    [
        ("2026-10-23T00:00:00Z", "observation_time_in_future"),
        ("2026-09-01T00:00:00", "observation_time_invalid"),
        ("yesterday", "observation_time_invalid"),
    ],
)
def test_invalid_or_future_observation_is_not_replaced_by_file_age(tmp_path, observed, reason):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/body.json").write_bytes(b"keep")
    result = plan_retention(tmp_path, [record(observed=observed)], now=NOW)
    assert result["entries"][0]["reason"] == reason
    assert result["actions"] == []
