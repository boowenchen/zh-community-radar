"""Native public adapters. Complete means the configured scan, never the entire platform."""

import calendar
import json
import re
from collections import deque
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import feedparser
from bs4 import BeautifulSoup

from .fetch import FetchError
from .models import Item, Scan, canonical_url


def plain(html):
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup.select("script,style,iframe,object"):
        node.decompose()
    return soup.get_text(" ", strip=True)


def epoch_time(value):
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def feed_time(entry, key):
    value = entry.get(key + "_parsed")
    return epoch_time(calendar.timegm(value)) if value else None


def source_time(value):
    """Missing or malformed upstream time is unknown, never the collection clock."""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, TypeError):
        return None


def fail_scan(scan, error):
    scan.status = "partial" if scan.items else error.status
    scan.error = error.code
    scan.retry_at = error.retry_at
    scan.complete = False
    return scan


def cache_is_stale(scan, headers, maximum):
    try:
        age = int(headers.get("age", "0"))
    except ValueError:
        scan.warnings.append("invalid_upstream_cache_age")
        return False
    if age > maximum:
        scan.status = "stale"
        scan.complete = False
        scan.warnings.append(f"upstream_cache_age:{age}")
        return True
    return False


def parse_feed(body: bytes, source_id: str, kind="post"):
    feed = feedparser.parse(body)
    if not feed.get("version"):
        raise FetchError("not_a_feed", "parse_error")
    items = []
    warnings = ["malformed_feed_recovered"] if feed.get("bozo") else []
    for entry in feed.entries:
        url = entry.get("link", "")
        try:
            canonical_url(url)
        except ValueError:
            warnings.append("invalid_entry_url")
            continue
        content = entry.get("content", [])
        text = plain(content[0].get("value", "") if content else entry.get("summary", ""))
        items.append(
            Item(
                source_id=source_id,
                native_id=str(entry.get("id") or url),
                url=url,
                title=plain(entry.get("title", ""))[:2000],
                text=text[:250000],
                author=entry.get("author"),
                kind=kind,
                published_at=feed_time(entry, "published"),
                source_updated_at=feed_time(entry, "updated"),
                metadata={
                    "content_scope": "feed_content" if content else "feed_summary",
                    "time_basis": "source",
                    "comments_available": False,
                },
            )
        )
    return items, warnings


def feed_scan(config, fetcher, checkpoint, known_ids):
    scan = Scan(config["id"], capabilities={"posts": True, "comments": False, "scope": "feed_window"})
    headers = {}
    if checkpoint.get("etag"):
        headers["If-None-Match"] = checkpoint["etag"]
    if checkpoint.get("last_modified"):
        headers["If-Modified-Since"] = checkpoint["last_modified"]
    body, response_headers, status = fetcher.get(config["url"], headers=headers)
    if cache_is_stale(scan, response_headers, config.get("max_cache_age_seconds", 7200)):
        return scan
    if status == 304:
        scan.complete = True
        scan.checkpoint = checkpoint
        scan.warnings.append("not_modified_does_not_prove_freshness")
        return scan
    scan.items, scan.warnings = parse_feed(body, config["id"], config.get("content_kind", "post"))
    if not scan.items:
        scan.warnings.append("valid_empty_feed")
    scan.checkpoint = {
        "etag": response_headers.get("etag"),
        "last_modified": response_headers.get("last-modified"),
        "head_ids": [i.native_id for i in scan.items[:5]],
    }
    old_heads = set(checkpoint.get("head_ids", []))
    if old_heads and scan.items and not old_heads.intersection(i.native_id for i in scan.items):
        scan.warnings.append("feed_window_no_overlap_possible_gap")
    scan.complete = True
    return scan


def parse_reddit_feed(body, source_id, root_id=None):
    """Public Atom is a bounded excerpt; it never supplies karma or reply hierarchy."""
    value = feedparser.parse(body)
    if not value.get("version"):
        raise FetchError("not_a_feed", "parse_error")
    items, warnings = [], ["reddit_rss_incomplete_no_karma_or_reply_hierarchy"]
    if value.get("bozo"):
        warnings.append("malformed_feed_recovered")
    for entry in value.entries:
        native = str(entry.get("id", ""))
        url = entry.get("link", "")
        parts = urlsplit(url)
        match = re.match(r"/r/[^/]+/comments/([a-z0-9]+)/", parts.path)
        if (
            not re.fullmatch(r"t[13]_[a-z0-9]+", native)
            or parts.scheme != "https"
            or parts.hostname != "www.reddit.com"
            or not match
        ):
            warnings.append("invalid_reddit_entry")
            continue
        inferred_root = "t3_" + match[1]
        if root_id and inferred_root != root_id:
            warnings.append("unexpected_reddit_thread")
            continue
        is_comment = native.startswith("t1_")
        if not is_comment and native != inferred_root:
            warnings.append("mismatched_reddit_post_id")
            continue
        content = entry.get("content", [])
        html = content[0].get("value", "") if content else entry.get("summary", "")
        soup = BeautifulSoup(html, "html.parser")
        blocks = soup.select("div.md")
        # Do not turn the author/footer, thumbnail alt, or [comments] label into evidence.
        text = "\n\n".join(plain(str(block)) for block in blocks)
        metadata = {
            "content_scope": "public_rss_excerpt",
            "content_complete": False,
            "account_age": None,
            "account_karma": None,
            "score": None,
            "comments_total": None,
            "reply_hierarchy_available": False,
            "auth": "none_public_rss_not_oauth",
        }
        if is_comment:
            metadata.update(root_id=inferred_root, sampling="thread_rss_window")
        if not blocks:
            metadata["body_missing"] = True
        items.append(
            Item(
                source_id,
                native,
                url,
                "" if is_comment else plain(entry.get("title", ""))[:2000],
                text[:250000],
                feed_time(entry, "published"),
                feed_time(entry, "updated"),
                kind="comment" if is_comment else "post",
                metadata=metadata,
            )
        )
    return items, warnings


def reddit_scan(config, fetcher, checkpoint, known_ids):
    subreddit = config["subreddit"]
    if not re.fullmatch(r"[A-Za-z0-9_]{2,40}", subreddit):
        raise FetchError("invalid_subreddit", "parse_error")
    thread_limit = max(0, min(config.get("comment_threads", 0), 2))
    scan = Scan(
        config["id"],
        capabilities={
            "posts": True,
            "comments": bool(thread_limit),
            "scope": "public_rss_window",
            "comments_live_verified": False,
            "comment_sampling": "thread_rss_window_no_hierarchy",
            "auth": "none_public_rss_not_oauth",
        },
    )
    try:
        body, headers, _ = fetcher.get(f"https://www.reddit.com/r/{subreddit}/new/.rss")
        if cache_is_stale(scan, headers, config.get("max_cache_age_seconds", 7200)):
            return scan
        parsed, warnings = parse_reddit_feed(body, scan.source_id)
        scan.warnings.extend(warnings)
        posts = [item for item in parsed if item.kind == "post"][: config.get("posts", 25)]
        scan.items.extend(posts)
        for post in posts[:thread_limit]:
            native = post.native_id.removeprefix("t3_")
            body, headers, _ = fetcher.get(f"https://www.reddit.com/r/{subreddit}/comments/{native}/.rss")
            if cache_is_stale(scan, headers, config.get("max_cache_age_seconds", 7200)):
                return scan
            parsed, warnings = parse_reddit_feed(body, scan.source_id, post.native_id)
            scan.warnings.extend(warnings)
            replies = [item for item in parsed if item.kind == "comment"]
            scan.items.extend(replies[: config.get("comments_per_thread", 10)])
            scan.warnings.append(f"sampled_comments:{post.native_id}:{len(replies)}:total_unknown")
        heads = [item.native_id for item in posts[:5]]
        if checkpoint.get("head_ids") and posts and not set(checkpoint["head_ids"]).intersection(heads):
            scan.warnings.append("feed_window_no_overlap_possible_gap")
        if not posts:
            scan.warnings.append("valid_empty_feed")
        if not thread_limit:
            scan.warnings.append("posts_only_comments_not_requested")
        scan.checkpoint = {"head_ids": heads}
        scan.complete = True
    except FetchError as error:
        fail_scan(scan, error)
    return scan


def hn_scan(config, fetcher, checkpoint, known_ids):
    base = "https://hacker-news.firebaseio.com/v0/"
    scan = Scan(
        config["id"],
        capabilities={
            "posts": True,
            "comments": True,
            "scope": "bounded_new_and_top",
            "comment_sampling": "breadth_first",
        },
    )
    new = fetcher.json(base + "newstories.json")
    top = fetcher.json(base + "topstories.json")
    if not isinstance(new, list) or not isinstance(top, list):
        raise FetchError("invalid_hn_listing", "parse_error")
    limit = config.get("new_limit", 30)
    head = checkpoint.get("head")
    if head in new:
        candidates = new[: new.index(head)]
    else:
        candidates = new[:limit]
        if head:
            scan.warnings.append("unrecoverable_gap_previous_head_not_in_window")
    pending = [n for n in candidates if str(n) not in known_ids]
    complete = len(pending) <= limit
    selected = list(dict.fromkeys(top[: config.get("top_limit", 15)] + pending[:limit]))
    comment_threads = 0
    try:
        for native in selected:
            value = fetcher.json(f"{base}item/{native}.json")
            if not isinstance(value, dict):
                scan.warnings.append(f"item_unavailable:{native}")
                complete = False
                continue
            if value.get("deleted") or value.get("dead"):
                scan.items.append(
                    Item(
                        scan.source_id,
                        str(native),
                        f"https://news.ycombinator.com/item?id={native}",
                        "[deleted or dead]",
                        "",
                        kind="tombstone",
                    )
                )
                continue
            discussion = f"https://news.ycombinator.com/item?id={native}"
            scan.items.append(
                Item(
                    scan.source_id,
                    str(native),
                    discussion,
                    value.get("title", ""),
                    plain(value.get("text", "")),
                    epoch_time(value["time"]) if value.get("time") else None,
                    author=value.get("by"),
                    metadata={
                        "external_url": value.get("url"),
                        "score": value.get("score"),
                        "comments_total": value.get("descendants"),
                        "sample_scope": "API story; external article not fetched",
                    },
                )
            )
            if value.get("kids") and comment_threads < config.get("comment_threads", 3):
                comment_threads += 1
                queue = deque(value["kids"])
                visited = set()
                while queue and len(visited) < config.get("comments_per_thread", 8):
                    cid = queue.popleft()
                    if cid in visited:
                        continue
                    visited.add(cid)
                    comment = fetcher.json(f"{base}item/{cid}.json")
                    if not isinstance(comment, dict):
                        scan.warnings.append(f"comment_unavailable:{cid}")
                        complete = False
                        continue
                    queue.extend(comment.get("kids", []))
                    if comment.get("deleted") or comment.get("dead"):
                        scan.items.append(
                            Item(
                                scan.source_id,
                                str(cid),
                                f"https://news.ycombinator.com/item?id={cid}",
                                "",
                                "",
                                kind="tombstone",
                                parent_id=str(comment.get("parent") or native),
                                metadata={"root_id": str(native), "deleted_or_dead": True},
                            )
                        )
                        continue
                    scan.items.append(
                        Item(
                            scan.source_id,
                            str(cid),
                            f"https://news.ycombinator.com/item?id={cid}",
                            "",
                            plain(comment.get("text", "")),
                            epoch_time(comment["time"]) if comment.get("time") else None,
                            kind="comment",
                            parent_id=str(comment.get("parent")),
                            metadata={
                                "root_id": str(native),
                                "sampling": "breadth_first",
                                "thread_total": value.get("descendants"),
                            },
                        )
                    )
                scan.warnings.append(f"sampled_comments:{native}:{len(visited)}")
    except FetchError as error:
        return fail_scan(scan, error)
    scan.complete = complete
    scan.checkpoint = {"head": new[0] if new else head}
    if not complete:
        scan.status = "partial"
        scan.warnings.append("incomplete_window_continue_from_saved_items")
    return scan


def parse_ptt_article(html, source_id, url, title):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#main-content")
    if not main:
        raise FetchError("missing_ptt_content", "parse_error")
    published = None
    author = None
    for line in main.select(".article-metaline"):
        tag, value = line.select_one(".article-meta-tag"), line.select_one(".article-meta-value")
        if tag and value and tag.get_text(strip=True) == "時間":
            try:
                dt = datetime.strptime(value.get_text(strip=True), "%a %b %d %H:%M:%S %Y")
                published = (
                    dt.replace(tzinfo=ZoneInfo("Asia/Taipei"))
                    .astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except ValueError:
                pass
        if tag and value and tag.get_text(strip=True) == "作者":
            author = value.get_text(strip=True)
    native = urlsplit(url).path.rsplit("/", 1)[-1]
    comments = []
    for idx, push in enumerate(main.select(".push")):
        content = push.select_one(".push-content")
        reaction, timing = push.select_one(".push-tag"), push.select_one(".push-ipdatetime")
        text = content.get_text(" ", strip=True).removeprefix(":").strip() if content else ""
        stamp = timing.get_text(" ", strip=True) if timing else None
        # Neither author handles nor raw IP-bearing timestamps are retained for comments.
        md = re.search(r"\b\d{1,2}/\d{1,2}\s+\d{2}:\d{2}\b", stamp or "")
        comments.append(
            Item(
                source_id,
                f"{native}:push:{idx}",
                url,
                "",
                text,
                kind="comment",
                parent_id=native,
                metadata={
                    "root_id": native,
                    "reaction": reaction.get_text(strip=True) if reaction else None,
                    "source_time_text": md.group() if md else None,
                    "time_precision": "month_day",
                    "comment_identity": "article_push_index",
                    "identity_limit": "source edits can reorder or remove visible pushes",
                },
            )
        )
        push.decompose()
    for node in main.select(".article-metaline,.article-metaline-right,.f2"):
        node.decompose()
    body = main.get_text("\n", strip=True)
    body = body.split("※ 發信站:")[0].strip()
    post = Item(
        source_id,
        native,
        url,
        title,
        body[:250000],
        published,
        author=author,
        metadata={
            "comments_fetched": len(comments),
            "time_basis": "article_header" if published else "unknown",
        },
    )
    return [post, *comments]


def ptt_scan(config, fetcher, checkpoint, known_ids):
    scan = Scan(
        config["id"],
        capabilities={
            "posts": True,
            "comments": True,
            "scope": "configured_pages",
            "comment_sampling": "visible_pushes",
        },
    )
    url = config["url"]
    links = {}
    try:
        for _ in range(config.get("pages", 2)):
            body, _, _ = fetcher.get(url)
            soup = BeautifulSoup(body, "html.parser")
            if not soup.select_one(".r-list-container"):
                raise FetchError("ptt_listing_or_login", "auth_required")
            for entry in soup.select(".r-ent"):
                if entry.find_previous_sibling(class_="r-list-sep"):
                    continue
                a = entry.select_one(".title a")
                if a and re.fullmatch(r"/bbs/[\w_]+/M\.[\w.]+\.html", a.get("href", "")):
                    links[urljoin(url, a["href"])] = a.get_text(" ", strip=True)
            prev = next((a for a in soup.select(".btn-group-paging a") if "上頁" in a.get_text()), None)
            if not prev:
                break
            url = urljoin(url, prev.get("href", ""))
        # Page traversal is newest page first, but each page is oldest article first.
        # Sort native epoch IDs across pages; a plain reverse would prioritize the previous page.
        candidates = sorted(
            links.items(),
            key=lambda pair: int(urlsplit(pair[0]).path.rsplit("/", 1)[-1].split(".")[1]),
            reverse=True,
        )
        unseen = [
            (url, title)
            for url, title in candidates
            if urlsplit(url).path.rsplit("/", 1)[-1] not in known_ids
        ]
        maximum = config.get("articles", 8)
        # Keep observing new replies even while the bounded discovery backlog drains.
        refresh = [(u, t) for u, t in candidates if urlsplit(u).path.rsplit("/", 1)[-1] in known_ids]
        refresh = refresh[: min(config.get("refresh_articles", 3), maximum - bool(unseen))]
        new_selected = unseen[: maximum - len(refresh)]
        selected = new_selected + refresh
        for url, title in selected:
            body, _, _ = fetcher.get(url)
            scan.items.extend(
                parse_ptt_article(body.decode("utf-8", errors="replace"), scan.source_id, url, title)
            )
        scan.complete = len(unseen) <= len(new_selected)
        scan.checkpoint = {"visible_ids": [urlsplit(u).path.rsplit("/", 1)[-1] for u in links]}
        old_visible = set(checkpoint.get("visible_ids", []))
        if old_visible and not old_visible.intersection(scan.checkpoint["visible_ids"]):
            scan.warnings.append("configured_pages_no_overlap_possible_gap")
        if not scan.complete:
            scan.status = "partial"
            scan.warnings.append("article_limit_pending_visible_items")
    except FetchError as error:
        fail_scan(scan, error)
    return scan


def lobsters_scan(config, fetcher, checkpoint, known_ids):
    scan = Scan(
        config["id"],
        capabilities={
            "posts": True,
            "comments": True,
            "scope": "newest_window_and_selected_threads",
            "comment_sampling": "upstream_order_bounded",
        },
    )
    try:
        rows = fetcher.json("https://lobste.rs/newest.json")
        if not isinstance(rows, list):
            raise FetchError("invalid_lobsters_listing", "parse_error")
        limit = config.get("stories", 25)
        rows = rows[:limit]
        for row in rows:
            native = str(row["short_id"])
            if not re.fullmatch(r"[a-z0-9]{1,12}", native):
                raise FetchError("invalid_lobsters_id", "parse_error")
            scan.items.append(
                Item(
                    scan.source_id,
                    native,
                    f"https://lobste.rs/s/{native}",
                    row["title"],
                    row.get("description_plain") or plain(row.get("description", "")),
                    source_time(row.get("created_at")),
                    author=row.get("submitter_user"),
                    metadata={
                        "external_url": row.get("url"),
                        "score": row.get("score"),
                        "comments_total": row.get("comment_count"),
                        "tags": row.get("tags", []),
                        "content_scope": "submission_description",
                    },
                )
            )
        selected = sorted(rows, key=lambda x: x.get("comment_count", 0), reverse=True)
        selected = [r for r in selected if r.get("comment_count", 0) > 0][: config.get("comment_threads", 3)]
        for row in selected:
            native = str(row["short_id"])
            detail = fetcher.json(f"https://lobste.rs/s/{native}.json")
            if not isinstance(detail, dict) or not isinstance(detail.get("comments"), list):
                raise FetchError("invalid_lobsters_comments", "parse_error")
            comments = detail["comments"]
            subset = comments[: config.get("comments_per_thread", 20)]
            for comment in subset:
                gone = comment.get("is_deleted") or comment.get("is_moderated")
                cid = str(comment["short_id"])
                if not re.fullmatch(r"[a-z0-9]{1,12}", cid):
                    raise FetchError("invalid_lobsters_comment_id", "parse_error")
                scan.items.append(
                    Item(
                        scan.source_id,
                        cid,
                        f"https://lobste.rs/c/{cid}",
                        "",
                        "" if gone else comment.get("comment_plain", plain(comment.get("comment", ""))),
                        source_time(comment.get("created_at")),
                        source_time(comment.get("last_edited_at")),
                        kind="tombstone" if gone else "comment",
                        parent_id=str(comment.get("parent_comment") or native),
                        metadata={
                            "root_id": native,
                            "score": comment.get("score"),
                            "sampling": "upstream_order_bounded",
                            "deleted_or_moderated": bool(gone),
                        },
                    )
                )
            if len(subset) < len(comments):
                scan.warnings.append(f"sampled_comments:{native}:{len(subset)}/{len(comments)}")
        ids = [str(r["short_id"]) for r in rows]
        if checkpoint.get("head_ids") and not set(checkpoint["head_ids"]).intersection(ids):
            scan.warnings.append("newest_window_no_overlap_possible_gap")
        scan.checkpoint = {"head_ids": ids[:5]}
        scan.complete = True
    except FetchError as error:
        fail_scan(scan, error)
    return scan


def hf_scan(config, fetcher, checkpoint, known_ids):
    repo = config["repo"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise FetchError("invalid_hf_repo", "parse_error")
    base = f"https://huggingface.co/api/models/{repo}/discussions"
    scan = Scan(
        config["id"],
        capabilities={
            "posts": True,
            "comments": True,
            "scope": "configured_model_discussion_window",
            "comment_sampling": "bounded_events",
            "includes_pull_requests": config.get("include_prs", False),
        },
    )
    try:
        value = fetcher.json(base + "?p=0")
        if not isinstance(value, dict) or not isinstance(value.get("discussions"), list):
            raise FetchError("invalid_hf_listing", "parse_error")
        rows = [r for r in value["discussions"] if config.get("include_prs") or not r.get("isPullRequest")]
        window = rows[: config.get("discussion_window", 10)]
        # Refresh high-comment threads as well as discover unseen threads.
        unseen = [r for r in window if str(r["num"]) not in known_ids]
        refresh = sorted(
            [r for r in window if str(r["num"]) in known_ids],
            key=lambda r: r.get("numComments", 0),
            reverse=True,
        )
        limit = config.get("threads", 3)
        refresh = refresh[: min(1, limit - bool(unseen))]
        selected = unseen[: limit - len(refresh)] + refresh
        for row in selected:
            native = str(int(row["num"]))
            detail = fetcher.json(f"{base}/{native}")
            if not isinstance(detail, dict) or not isinstance(detail.get("events"), list):
                raise FetchError("invalid_hf_discussion", "parse_error")
            comments = [e for e in detail["events"] if e.get("type") == "comment"]
            visible = [e for e in comments if not e.get("data", {}).get("hidden")]
            first = comments[0] if comments else None
            body = first["data"]["latest"].get("raw", "") if first in visible else ""
            public_url = f"https://huggingface.co/{repo}/discussions/{native}"
            scan.items.append(
                Item(
                    scan.source_id,
                    native,
                    public_url,
                    detail["title"],
                    body[:250000],
                    source_time(detail.get("createdAt")),
                    source_time(first["data"]["latest"].get("updatedAt")) if first in visible else None,
                    metadata={
                        "repo": repo,
                        "discussion_status": detail.get("status"),
                        "is_pull_request": bool(detail.get("isPullRequest")),
                        "content_scope": "opening_comment",
                        "comments_total": len(comments),
                        "hidden_comments": len(comments) - len(visible),
                    },
                )
            )
            replies = [e for e in visible if e is not first]
            subset = replies[: config.get("comments_per_thread", 20)]
            for event in subset:
                cid = str(event["id"])
                latest = event["data"]["latest"]
                scan.items.append(
                    Item(
                        scan.source_id,
                        f"{native}:{cid}",
                        public_url + f"#{cid}",
                        "",
                        latest.get("raw", "")[:250000],
                        source_time(event.get("createdAt")),
                        source_time(latest.get("updatedAt")),
                        kind="comment",
                        parent_id=native,
                        metadata={
                            "root_id": native,
                            "sampling": "bounded_events",
                            "content_format": "markdown",
                        },
                    )
                )
            if len(subset) < len(replies):
                scan.warnings.append(f"sampled_comments:{native}:{len(subset)}/{len(replies)}")
        scan.complete = len(unseen) <= limit - len(refresh)
        scan.checkpoint = {"visible_ids": [str(r["num"]) for r in window]}
        scan.warnings.append(f"configured_discussion_window:{len(window)}/{value.get('count', 'unknown')}")
        if not scan.complete:
            scan.status = "partial"
            scan.warnings.append("thread_limit_pending_window_items")
    except FetchError as error:
        fail_scan(scan, error)
    return scan


def v2ex_scan(config, fetcher, checkpoint, known_ids):
    scan = Scan(
        config["id"],
        capabilities={
            "posts": True,
            "comments": True,
            "scope": "public_v1_latest_window",
            "comment_sampling": "first_reply_page_bounded",
            "auth": "none_public_v1_not_v2",
        },
    )
    maximum_age = config.get("max_cache_age_seconds", 1800)
    try:
        body, headers, _ = fetcher.get("https://www.v2ex.com/api/topics/latest.json")
        if cache_is_stale(scan, headers, maximum_age):
            return scan
        rows = json.loads(body)
        if not isinstance(rows, list):
            raise FetchError("invalid_v2ex_listing", "parse_error")
        rows = rows[: config.get("stories", 40)]
        for row in rows:
            native = str(int(row["id"]))
            scan.items.append(
                Item(
                    scan.source_id,
                    native,
                    f"https://www.v2ex.com/t/{native}",
                    row["title"],
                    row.get("content") or plain(row.get("content_rendered", "")),
                    epoch_time(row["created"]) if row.get("created") else None,
                    epoch_time(row["last_modified"]) if row.get("last_modified") else None,
                    metadata={
                        "comments_total": row.get("replies"),
                        "node": row.get("node", {}).get("name"),
                        "content_scope": "public_v1_content",
                        "cache_age": headers.get("age"),
                    },
                )
            )
        selected = sorted(rows, key=lambda r: r.get("replies", 0), reverse=True)
        selected = [r for r in selected if r.get("replies", 0) > 0][: config.get("comment_threads", 3)]
        for row in selected:
            native = str(int(row["id"]))
            body, headers, _ = fetcher.get(
                f"https://www.v2ex.com/api/replies/show.json?topic_id={native}&page=1"
            )
            if cache_is_stale(scan, headers, maximum_age):
                return scan
            replies = json.loads(body)
            if not isinstance(replies, list):
                raise FetchError("invalid_v2ex_replies", "parse_error")
            subset = replies[: config.get("comments_per_thread", 20)]
            for reply in subset:
                cid = str(int(reply["id"]))
                scan.items.append(
                    Item(
                        scan.source_id,
                        f"reply:{cid}",
                        f"https://www.v2ex.com/t/{native}",
                        "",
                        reply.get("content") or plain(reply.get("content_rendered", "")),
                        epoch_time(reply["created"]) if reply.get("created") else None,
                        epoch_time(reply["last_modified"]) if reply.get("last_modified") else None,
                        kind="comment",
                        parent_id=native,
                        metadata={
                            "root_id": native,
                            "sampling": "first_reply_page_bounded",
                            "thread_total": row.get("replies"),
                            "cache_age": headers.get("age"),
                        },
                    )
                )
            scan.warnings.append(f"sampled_comments:{native}:{len(subset)}/{row.get('replies', 'unknown')}")
        ids = [str(r["id"]) for r in rows]
        if checkpoint.get("head_ids") and not set(checkpoint["head_ids"]).intersection(ids):
            scan.warnings.append("latest_window_no_overlap_possible_gap")
        scan.checkpoint = {"head_ids": ids[:5]}
        scan.complete = True
    except FetchError as error:
        fail_scan(scan, error)
    except (ValueError, UnicodeDecodeError):
        fail_scan(scan, FetchError("invalid_v2ex_json", "parse_error"))
    return scan


ADAPTERS = {
    "feed": feed_scan,
    "hn": hn_scan,
    "ptt": ptt_scan,
    "lobsters": lobsters_scan,
    "hf": hf_scan,
    "v2ex": v2ex_scan,
    "reddit_rss": reddit_scan,
}
