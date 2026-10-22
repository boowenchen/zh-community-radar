"""Incremental projections over observed item revisions, with bounded evidence samples."""

import hashlib
import json
import re
from itertools import groupby
from urllib.parse import urlsplit

from .models import canonical_url, json_text, normalized_time, stable_id, utcnow

PROJECTION_VERSION = "2"
MEMBER_SAMPLE_LIMIT = 200
TOPICS = {
    "AI 模型與應用": r"\b(llm|ai|inference|model|agent|gpu|cuda|ollama|vllm|transformer|claude|gpt)\b|人工智慧|模型|推論",
    "軟體與開源": r"\b(linux|python|rust|github|software|developer|programming|database|release|compiler|security)\b|軟體|開源|程式|資安",
    "產業金融": r"\b(stock|valuation|revenue|semiconductor|datacenter|earnings|nvidia|tsmc)\b|股價|估值|財報|台積|半導體",
    "硬體": r"\b(hardware|cpu|memory|ram|chip|laptop)\b|硬體|顯卡|記憶體",
}


def anchor(item):
    url = item["metadata"].get("external_url") or item["url"]
    try:
        normalized = canonical_url(url)
    except (ValueError, TypeError):
        normalized = canonical_url(item["url"])
    if urlsplit(normalized).hostname == "github.com":
        normalized = normalized.replace("/releases/tag/", "/tree/")
    return normalized


def _version_item(db, item_id, revision):
    row = db.conn.execute(
        """SELECT v.*,c.context,i.source_id,i.native_id FROM item_versions v
        JOIN items i USING(item_id) LEFT JOIN item_context_versions c USING(item_id,revision)
        WHERE v.item_id=? AND v.revision=?""",
        (item_id, revision),
    ).fetchone()
    if row is None:
        raise ValueError("event_evidence_missing")
    item = dict(row)
    context = item.pop("context")
    if context:
        item.update(json.loads(context))
        item["context_missing"] = False
    else:
        # Only source/native identifiers are invariant. Today's URL/kind are not history.
        item.update(
            url=None,
            kind="unknown",
            published_at=None,
            parent_id=None,
            metadata={"context_provenance": "legacy_unverified"},
            context_missing=True,
        )
    item.setdefault("parent_id", None)
    item.setdefault("first_seen_at", item["observed_at"])
    item["fetched_at"] = item["observed_at"]
    return item


def _reassign(db, item, affected):
    old = db.conn.execute("SELECT event_id FROM event_members WHERE item_id=?", (item["item_id"],)).fetchone()
    if old:
        affected.add(old[0])
    db.conn.execute("DELETE FROM event_members WHERE item_id=?", (item["item_id"],))
    if item["kind"] in {"tombstone", "unknown"}:
        return
    root = item
    if item["kind"] == "comment":
        root_id = item["metadata"].get("root_id") or item["parent_id"]
        found = db.conn.execute(
            """SELECT s.observed_at,v.title,c.context FROM event_item_state s
            JOIN item_versions v USING(item_id,revision) JOIN item_context_versions c USING(item_id,revision)
            WHERE s.source_id=? AND s.native_id=? AND s.kind NOT IN ('comment','tombstone','unknown')""",
            (item["source_id"], root_id),
        ).fetchone()
        if not found:
            return
        root = json.loads(found["context"])
        root.update(title=found["title"])
        root.setdefault("first_seen_at", found["observed_at"])
    key = anchor(root)
    eid = stable_id("event", key)
    db.conn.execute(
        "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?)",
        (eid, key, root["title"], 0, "", root["first_seen_at"], item["observed_at"], 0, "{}"),
    )
    db.conn.execute("INSERT INTO event_members VALUES (?,?)", (item["item_id"], eid))
    affected.add(eid)


def _project_event(db, eid, changed_at):
    old = db.conn.execute("SELECT * FROM events WHERE event_id=?", (eid,)).fetchone()
    digest = hashlib.sha256()
    posts, comments, primary_length, primary_title = 0, 0, -1, "[來源已刪除或移動]"
    topics, sources = set(), set()
    post_sample, comment_sample = [], []
    rich_evidence = False
    # Stream IDs, then one immutable row at a time. A large discussion cannot fill Python RAM.
    states = db.conn.execute(
        """SELECT s.item_id,s.revision FROM event_item_state s JOIN event_members m USING(item_id)
        WHERE m.event_id=? ORDER BY s.item_id""",
        (eid,),
    )
    for state in states:
        item = _version_item(db, state["item_id"], state["revision"])
        digest.update(f"{item['item_id']}:{item['revision']}\n".encode())
        sources.add(item["source_id"])
        member = {key: item[key] for key in ("item_id", "revision", "content_hash")}
        if item["kind"] == "comment":
            comments += 1
            if len(comment_sample) < MEMBER_SAMPLE_LIMIT:
                comment_sample.append(member)
        elif item["kind"] not in {"tombstone", "unknown"}:
            posts += 1
            if len(post_sample) < MEMBER_SAMPLE_LIMIT:
                post_sample.append(member)
            if len(item["text"]) > primary_length:
                primary_length, primary_title = len(item["text"]), item["title"]
            rich_evidence |= len(item["text"]) >= 200
            text = item["title"] + " " + item["text"][:1200]
            topics.update(name for name, pattern in TOPICS.items() if re.search(pattern, text, re.IGNORECASE))
    fingerprint = digest.hexdigest()
    if old["fingerprint"] == fingerprint:
        return None
    parts = {
        "relevance": 2 if topics else 0,
        "increment": 1,
        "evidence": 2 if rich_evidence else 1,
        "utility": 1 if topics else 0,
        "independent_verification": 0,
    }
    members = (post_sample + comment_sample)[:MEMBER_SAMPLE_LIMIT]
    details = {
        "topics": [name for name in TOPICS if name in topics],
        "score_parts": parts,
        "item_count": posts,
        "comment_count": comments,
        "independent_authors": None,
        "independent_verification": "not_assessed",
        "source_ids": sorted(sources),
        "sampling_limit": "取得樣本，非全社群；未計算獨立作者數",
        "baseline": "provisional",
        "member_count": posts + comments,
        "member_sample_limit": MEMBER_SAMPLE_LIMIT,
        "member_sample_truncated": posts + comments > len(members),
    }
    # Removal/move is itself a new observation, even when surviving members are old.
    observed = max(changed_at, old["updated_at"])
    revision = old["revision"] + 1
    snapshot = {
        "event_id": eid,
        "revision": revision,
        "anchor": old["anchor"],
        "title": primary_title,
        "first_seen_at": old["first_seen_at"],
        "updated_at": observed,
        "score": sum(parts.values()),
        "retired": not posts,
        **details,
        "members": members,
    }
    db.conn.execute(
        """UPDATE events SET title=?,revision=?,fingerprint=?,updated_at=?,score=?,details=? WHERE event_id=?""",
        (primary_title, revision, fingerprint, observed, snapshot["score"], json_text(details), eid),
    )
    db.conn.execute(
        "INSERT INTO event_versions VALUES (?,?,?,?)", (eid, revision, observed, json_text(snapshot))
    )
    return {"event_id": eid, "revision": revision}


def rebuild(db, batch_size=5000):
    cursor_row = db.conn.execute("SELECT value FROM meta WHERE key='event_cursor'").fetchone()
    cursor = int(cursor_row[0]) if cursor_row else 0
    projection = db.conn.execute("SELECT value FROM meta WHERE key='event_projection_version'").fetchone()
    if cursor and (not projection or projection[0] != PROJECTION_VERSION):
        raise ValueError("event_state_migration_required")
    changes = db.conn.execute(
        "SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT ?",
        (cursor, max(1, min(batch_size, 5000))),
    ).fetchall()
    watermark = db.conn.execute("SELECT value FROM meta WHERE key='event_observed_watermark'").fetchone()
    previous = (
        watermark[0]
        if watermark
        else db.conn.execute("SELECT max(observed_at) FROM event_item_state").fetchone()[0]
    )
    for change in changes:
        observed = normalized_time(change["observed_at"])
        if previous and observed < previous:
            raise ValueError("event_observation_order_invalid")
        previous = observed
    changed = {}
    with db.conn:
        # Equal observation instants can coalesce; distinct instants retain their history.
        for observed, group in groupby(changes, key=lambda change: change["observed_at"]):
            affected = set()
            for change in group:
                item = _version_item(db, change["item_id"], change["revision"])
                db.conn.execute(
                    """INSERT INTO event_item_state VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(item_id)
                    DO UPDATE SET revision=excluded.revision,source_id=excluded.source_id,native_id=excluded.native_id,
                    kind=excluded.kind,parent_id=excluded.parent_id,root_id=excluded.root_id,observed_at=excluded.observed_at""",
                    (
                        item["item_id"],
                        item["revision"],
                        item["source_id"],
                        item["native_id"],
                        item["kind"],
                        item["parent_id"],
                        item["metadata"].get("root_id"),
                        item["observed_at"],
                    ),
                )
                _reassign(db, item, affected)
                # A former post may itself be reclassified as a comment; its children
                # must still detach instead of retaining a retired root's membership.
                children = db.conn.execute(
                    """SELECT item_id,revision FROM event_item_state WHERE source_id=? AND kind='comment'
                    AND (root_id=? OR parent_id=?)""",
                    (item["source_id"], item["native_id"], item["native_id"]),
                )
                for child in children:
                    _reassign(db, _version_item(db, child["item_id"], child["revision"]), affected)
            for eid in sorted(affected):
                update = _project_event(db, eid, observed)
                if update:
                    changed[eid] = update
        next_cursor = changes[-1]["seq"] if changes else cursor
        for key, value in (
            ("event_cursor", str(next_cursor)),
            ("event_projection_version", PROJECTION_VERSION),
            ("event_observed_watermark", previous or ""),
        ):
            db.conn.execute(
                "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
    pending = db.conn.execute("SELECT count(*) FROM changes WHERE seq>?", (next_cursor,)).fetchone()[0]
    orphan_comments = db.conn.execute(
        """SELECT count(*) FROM event_item_state s WHERE kind='comment'
        AND NOT EXISTS (SELECT 1 FROM event_members m WHERE m.item_id=s.item_id)""",
    ).fetchone()[0]
    unknown_context = db.conn.execute(
        "SELECT count(*) FROM event_item_state WHERE kind='unknown'"
    ).fetchone()[0]
    return {
        "changed": bool(changed),
        "changed_events": len(changed),
        "events": list(changed.values()),
        "model_required": bool(changed),
        "notify": False,
        "orphan_comments": orphan_comments,
        "unknown_context_items": unknown_context,
        "processed_changes": len(changes),
        "next_cursor": next_cursor,
        "pending_changes": pending,
        "grouping": "exact_origin_v2",
        "semantic_merge": "pending_independent_validation",
    }


def list_events(db, cutoff=None, since=None, limit=100, event_id=None):
    cutoff = normalized_time(cutoff or utcnow())
    since = normalized_time(since) if since else None
    query = """SELECT snapshot FROM event_versions ev WHERE observed_at<=?
        AND revision=(SELECT max(v.revision) FROM event_versions v WHERE v.event_id=ev.event_id AND v.observed_at<=?)
        AND coalesce(json_extract(snapshot,'$.retired'),0)=0"""
    params = [cutoff, cutoff]
    if event_id:
        query += " AND ev.event_id=?"
        params.append(event_id)
    if since:
        query += " AND observed_at>=?"
        params.append(since)
    query += (
        " ORDER BY CAST(json_extract(snapshot,'$.score') AS INTEGER) DESC,observed_at DESC,event_id LIMIT ?"
    )
    params.append(max(1, min(int(limit), 1000)))
    return [json.loads(row[0]) for row in db.conn.execute(query, params)]


def event_evidence(db, event, max_chars=16000):
    result, used = [], 0
    max_chars = max(0, min(int(max_chars), 100_000))
    if not max_chars:
        return result
    for member in event["members"]:
        item = _version_item(db, member["item_id"], member["revision"])
        if item["content_hash"] != member["content_hash"]:
            raise ValueError("event_evidence_missing")
        original_length = len(item["text"])
        item["text"] = item["text"][: min(4000, max_chars - used)]
        used += len(item["text"])
        item["excerpt_truncated"] = len(item["text"]) < original_length
        result.append(item)
        if used >= max_chars:
            break
    return result
