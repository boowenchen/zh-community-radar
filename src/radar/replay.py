"""Read-only legacy ingestion and frozen evaluation inputs. Never imports legacy code."""

import argparse
import hashlib
import itertools
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .models import Item, Scan, canonical_url, json_text, stable_id, utcnow
from .store import Store

FIELDS = (
    "uid",
    "source",
    "grp",
    "role",
    "platform",
    "title",
    "url",
    "canon_url",
    "summary",
    "published",
    "seen_at",
    "refreshed_at",
    "fetched_at",
    "license_class",
)
PROJECTION_VERSION = "2"


def timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("legacy_timezone_missing")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def import_legacy(source_path, destination):
    """Whitelist import into a dedicated empty store; imported summaries stay summaries."""
    source_path, destination = Path(source_path).resolve(), Path(destination).resolve()
    if source_path == destination / "radar.sqlite3":
        raise ValueError("legacy_destination_is_source")
    before = hashlib.sha256(source_path.read_bytes()).hexdigest()
    with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as old:
        old.execute("PRAGMA query_only=ON")
        old.row_factory = sqlite3.Row
        # Column names are a constant whitelist, not user input. No extra/authors/fetch_path.
        records = [dict(r) for r in old.execute("SELECT " + ",".join(FIELDS) + " FROM items ORDER BY uid")]
    dataset_hash = hashlib.sha256(json_text(records).encode()).hexdigest()
    manifest_path = destination / "legacy-manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous["dataset_sha256"] != dataset_hash:
            raise ValueError("legacy_dataset_changed_use_new_destination")
        if not (destination / "radar.sqlite3").is_file():
            raise ValueError("legacy_destination_missing_database")
        with sqlite3.connect((destination / "radar.sqlite3").as_uri() + "?mode=ro", uri=True) as existing:
            if existing.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("legacy_destination_corrupt")
            if existing.execute("SELECT count(*) FROM items").fetchone()[0] != previous["imported"]:
                raise ValueError("legacy_destination_item_count_changed")
            projection = existing.execute(
                "SELECT value FROM meta WHERE key='legacy_projection_version'"
            ).fetchone()
            if not projection or projection[0] != PROJECTION_VERSION:
                raise ValueError("legacy_projection_requires_rebuild")
            if existing.execute(
                "SELECT 1 FROM (SELECT observed_at,lag(observed_at) OVER (ORDER BY seq) AS previous "
                "FROM changes) WHERE observed_at < previous LIMIT 1"
            ).fetchone():
                raise ValueError("legacy_projection_not_monotonic")
        return previous
    if (destination / "radar.sqlite3").exists():
        raise ValueError("legacy_import_requires_empty_destination")
    groups = defaultdict(list)
    observed_rows, skipped, first_seen = [], [], {}
    future_publication_count = 0
    for row in records:
        try:
            seen = timestamp(row["seen_at"])
            if not seen:
                raise ValueError("legacy_missing_seen_time")
            observed = max(filter(None, [seen, timestamp(row["refreshed_at"]), timestamp(row["fetched_at"])]))
            published = timestamp(row["published"])
            future = bool(published and published > observed)
            future_publication_count += int(future)
            url = canonical_url(row["url"] or row["canon_url"])
            metadata = {
                "content_scope": "legacy_summary",
                "evidence_kind": "historical_summary_snapshot",
                "original_text_available": False,
                "comments_available": False,
                "legacy_source": row["source"],
                "legacy_group": row["grp"],
                "legacy_role": row["role"],
                "platform": row["platform"],
                "license_class": row["license_class"],
                "legacy_first_seen_at": seen,
                "snapshot_observed_at": observed,
                "time_basis": "latest_known_snapshot_not_first_seen",
                "legacy_published_at": published,
                "publication_in_future_at_observation": future,
            }
            item = Item(
                "legacy-" + row["source"],
                row["uid"],
                url,
                (row["title"] or "")[:2000],
                (row["summary"] or "")[:250000],
                None if future else published,
                fetched_at=observed,
                metadata=metadata,
            ).validate()
            groups[(observed, item.source_id)].append(item)
            first_seen[item.item_id] = seen
            observed_rows.append(
                {**item.as_dict(), "item_id": item.item_id, "first_seen_at": seen, "observed_at": observed}
            )
        except (ValueError, TypeError, KeyError):
            skipped.append({"uid": row["uid"], "source": row["source"], "reason": "invalid_required_field"})
    with Store(destination) as store:
        # changes.seq is consumed chronologically by event projection. Grouping only
        # by source incorrectly puts a source's newest snapshot before older peers.
        for (observed, source_id), items in sorted(groups.items()):
            items.sort(key=lambda item: item.native_id)
            scan = Scan(
                source_id,
                items=items,
                complete=True,
                observed_at=observed,
                host="legacy-offline-replay",
                requested_egress="none",
                effective_egress="none",
                capabilities={"historical_summary": True, "live": False, "comments": False},
                warnings=["legacy_summary_is_not_full_text", "not_live_source_acceptance"],
            )
            group_hash = stable_id(dataset_hash, observed, source_id, *(item.item_id for item in items))
            store.save_scan(scan, "legacy-" + group_hash)
        with store.conn:
            store.conn.executemany(
                "UPDATE items SET first_seen_at=? WHERE item_id=?",
                [(seen, item_id) for item_id, seen in first_seen.items()],
            )
            # Store initially knows fetched_at only. Apply the real legacy discovery
            # time to both the current row and its immutable snapshot context.
            contexts = []
            for row in store.conn.execute("SELECT item_id,revision,context FROM item_context_versions"):
                context = json.loads(row["context"])
                context["first_seen_at"] = first_seen[row["item_id"]]
                contexts.append((json_text(context), row["item_id"], row["revision"]))
            store.conn.executemany(
                "UPDATE item_context_versions SET context=? WHERE item_id=? AND revision=?", contexts
            )
            store.conn.execute("INSERT OR REPLACE INTO meta VALUES ('operating_mode','shadow')")
            store.conn.execute("INSERT OR REPLACE INTO meta VALUES ('data_origin','legacy-replay')")
            store.conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('legacy_projection_version',?)", (PROJECTION_VERSION,)
            )
    after = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if before != after:
        raise ValueError("legacy_source_changed_during_read")
    manifest = {
        "created_at": utcnow(),
        "source_path": str(source_path),
        "source_file_sha256": before,
        "source_unchanged_after_read": True,
        "dataset_sha256": dataset_hash,
        "selected_fields": FIELDS,
        "imported": len(observed_rows),
        "source_rows": len(records),
        "skipped": skipped,
        "future_publication_count": future_publication_count,
        "original_text_available": False,
        "seen_range": [min(first_seen.values(), default=None), max(first_seen.values(), default=None)],
        "snapshot_observation_range": [
            min((r["observed_at"] for r in observed_rows), default=None),
            max((r["observed_at"] for r in observed_rows), default=None),
        ],
        "source_counts": dict(Counter(r["source_id"] for r in observed_rows)),
        "limitations": [
            "legacy summaries are not source full text",
            "no old story ID is used as gold",
            "latest snapshot cannot prove what text existed at original first_seen",
            "historical import is not a live source run",
            "delivery remains shadow",
        ],
    }
    (destination / "legacy-items.json").write_text(
        json.dumps(observed_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_windows(rows, count=5):
    instants = sorted({timestamp(r["observed_at"]) for r in rows})
    if len(instants) < count or count < 1:
        raise ValueError("insufficient_distinct_observation_times")
    cutoffs = [instants[math.ceil(len(instants) * n / count) - 1] for n in range(1, count + 1)]
    windows, previous = [], None
    for i, cutoff in enumerate(cutoffs):
        new = [
            r["item_id"]
            for r in rows
            if (previous is None or r["observed_at"] > previous) and r["observed_at"] <= cutoff
        ]
        windows.append(
            {
                "window_id": f"legacy-window-{i + 1}",
                "since_exclusive": previous,
                "cutoff": cutoff,
                "newly_visible_count": len(new),
                "item_ids": sorted(new),
                "type": "true_snapshot_observation_window_not_calendar_day",
                "live": False,
            }
        )
        previous = cutoff
    return windows


def pair_candidates(rows, count=40):
    """Deterministic diversity sampling only. An independent reviewer supplies labels."""
    rows = sorted(rows, key=lambda r: r["item_id"])
    by_id = {r["item_id"]: r for r in rows}
    url_groups, token_groups = defaultdict(list), defaultdict(list)
    tokens = {}
    generic = {
        "release",
        "released",
        "releases",
        "available",
        "model",
        "models",
        "text-generation",
        "with",
        "that",
        "this",
        "your",
        "from",
        "have",
        "what",
        "code",
        "2026-10-12",
        "2026-10-11",
        "2026-10-13",
    }
    for row in rows:
        url_groups[canonical_url(row["url"])].append(row["item_id"])
        tokens[row["item_id"]] = set(re.findall(r"[a-z][a-z0-9_.-]{3,}", row["title"].lower())) - generic
        for token in tokens[row["item_id"]]:
            token_groups[token].append(row["item_id"])
    buckets = defaultdict(set)
    for group in url_groups.values():
        buckets["same_canonical_url"].update(itertools.combinations(sorted(group)[:10], 2))
    considered = set()
    contrast = re.compile(r"\b(not|fails?|broken|worse|better|disappoint|overhyped)\b", re.IGNORECASE)
    for group in token_groups.values():
        if not 2 <= len(group) <= 35:
            continue
        for a, b in itertools.combinations(sorted(group), 2):
            if (a, b) in considered:
                continue
            considered.add((a, b))
            union = tokens[a] | tokens[b]
            similarity = len(tokens[a] & tokens[b]) / len(union) if union else 0
            if similarity < 0.22:
                continue
            av = set(re.findall(r"\b[vV]?\d+(?:\.\d+)+\b", by_id[a]["title"]))
            bv = set(re.findall(r"\b[vV]?\d+(?:\.\d+)+\b", by_id[b]["title"]))
            kind = (
                "different_version_tokens"
                if av and bv and av != bv
                else (
                    "contrast_word_present"
                    if contrast.search(by_id[a]["title"] + " " + by_id[b]["title"])
                    else "similar_title_tokens"
                )
            )
            buckets[kind].add((a, b))
    # Low-similarity controls are selected from real rows, not invented events.
    for a, b in zip(rows, rows[len(rows) // 2 :]):
        if a["item_id"] != b["item_id"] and a["source_id"] != b["source_id"]:
            buckets["cross_source_control"].add(tuple(sorted((a["item_id"], b["item_id"]))))
    selected, used, item_uses, used_url_groups = [], set(), Counter(), set()
    quotas = [
        ("same_canonical_url", 10),
        ("different_version_tokens", 8),
        ("contrast_word_present", 4),
        ("similar_title_tokens", 14),
        ("cross_source_control", 4),
    ]

    def take(kind, maximum):
        def order(pair):
            a, b = pair
            union = tokens[a] | tokens[b]
            similarity = len(tokens[a] & tokens[b]) / len(union) if union else 0
            return by_id[a]["source_id"] == by_id[b]["source_id"], -similarity, pair

        for pair in sorted(buckets[kind], key=order):
            if pair in used:
                continue
            a, b = pair
            if item_uses[a] >= 2 or item_uses[b] >= 2:
                continue
            common_url = canonical_url(by_id[a]["url"])
            if kind == "same_canonical_url" and common_url in used_url_groups:
                continue
            used.add(pair)
            item_uses.update(pair)
            if kind == "same_canonical_url":
                used_url_groups.add(common_url)
            selected.append(
                {
                    "pair_id": stable_id(a, b),
                    "sampling_feature": kind,
                    "split": "holdout",
                    "review_status": "unlabeled",
                    "synthetic": False,
                    "a": by_id[a],
                    "b": by_id[b],
                }
            )
            maximum -= 1
            if not maximum or len(selected) >= count:
                break

    for kind, quota in quotas:
        if len(selected) >= count:
            break
        take(kind, min(quota, count - len(selected)))
    for kind, _ in quotas:
        if len(selected) >= count:
            break
        take(kind, count - len(selected))
    if len(selected) != count:
        raise ValueError("insufficient_candidate_pairs")
    return selected


def baseline(days, as_of_date, window_days=28):
    """Only completed source-days enter the denominator; missing is not zero."""
    cutoff = date.fromisoformat(as_of_date)
    start = cutoff - timedelta(days=window_days)
    seen, valid = set(), []
    excluded_future = 0
    for row in days:
        day = date.fromisoformat(row["date"])
        if day in seen:
            raise ValueError("duplicate_day")
        seen.add(day)
        if day >= cutoff:
            excluded_future += 1
            continue
        if day < start:
            continue
        value = row.get("count")
        if row.get("status") == "complete" and value is not None:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("invalid_complete_day_count")
            valid.append(value)
    total = sum(valid)
    return {
        "window_days": window_days,
        "start_inclusive": start.isoformat(),
        "cutoff_exclusive": cutoff.isoformat(),
        "effective_days": len(valid),
        "missing_or_incomplete_days": window_days - len(valid),
        "total_observed": total,
        "mean_per_effective_day": total / len(valid) if valid else None,
        "provisional": len(valid) < window_days,
        "zero_baseline": bool(valid) and total == 0,
        "excluded_current_or_future": excluded_future,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    destination = Path(args.destination)
    manifest = import_legacy(args.source, destination)
    rows = json.loads((destination / "legacy-items.json").read_text(encoding="utf-8"))
    windows, pairs = build_windows(rows), pair_candidates(rows)
    for name, value in [("windows.json", windows), ("pair-candidates.json", pairs)]:
        (destination / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "manifest": str(destination / "legacy-manifest.json"),
                "imported": manifest["imported"],
                "windows": len(windows),
                "unlabeled_pairs": len(pairs),
                "pair_sampling": dict(Counter(p["sampling_feature"] for p in pairs)),
            }
        )
    )


if __name__ == "__main__":
    main()
