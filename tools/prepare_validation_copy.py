"""Rebuild derived events in a NEW shadow copy; never delete projection state in the source DB."""

import argparse
import json
from pathlib import Path

from radar.events import rebuild
from radar.recovery import atomic_write_json, restore_snapshot
from radar.store import Store


def prepare(source, snapshot, destination, config_paths=None):
    source, snapshot, destination = (Path(p).resolve() for p in (source, snapshot, destination))
    if len({source, snapshot, destination}) != 3 or snapshot.exists() or destination.exists():
        raise ValueError("new_distinct_validation_paths_required")
    with Store(source) as db:
        db.backup(snapshot, config_paths=config_paths)
    restored = restore_snapshot(snapshot, destination, config_paths=config_paths, require_program_match=True)
    with Store(destination) as db:
        tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ("reports", "assessments", "outbox"):
            if name in tables and db.conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]:
                raise ValueError("copy_has_derived_consumers_requires_separate_migration")
        before = {
            name: db.conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("items", "item_versions", "changes", "sources", "receipts")
        }
        with db.conn:
            for name in ("event_members", "event_versions", "event_item_state", "events"):
                db.conn.execute(f"DELETE FROM {name}")
            db.conn.execute(
                "DELETE FROM meta WHERE key IN ('event_cursor','event_projection_version','event_observed_watermark')"
            )
            db.conn.execute("INSERT OR REPLACE INTO meta VALUES ('operating_mode','shadow')")
        batches = []
        while True:
            result = rebuild(db)
            batches.append({k: v for k, v in result.items() if k != "events"})
            if not result["pending_changes"]:
                break
        after = {name: db.conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in before}
        if before != after:
            raise ValueError("source_data_changed_during_projection")
        event_count = db.conn.execute("SELECT count(*) FROM events").fetchone()[0]
    result = {
        "status": "shadow_ready",
        "source": str(source),
        "snapshot": str(snapshot),
        "destination": str(destination),
        "source_tables_preserved": before,
        "source_snapshot_sha256": restored["sha256"]
        if "sha256" in restored
        else restored.get("source_sha256"),
        "event_count": event_count,
        "batches": batches,
        "warning": "Old rows lacking immutable context remain unknown until a new source observation.",
    }
    atomic_write_json(destination / "projection-rebuild.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--source-config")
    parser.add_argument("--model-config")
    args = parser.parse_args()
    configs = {
        name: path for name, path in (("sources", args.source_config), ("model", args.model_config)) if path
    }
    print(
        json.dumps(prepare(args.source, args.snapshot, args.destination, configs or None), ensure_ascii=False)
    )
