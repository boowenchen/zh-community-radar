import httpx
import pytest

from radar.fetch import Fetcher, FetchError
from radar.sources import parse_feed, parse_ptt_article


def test_reddit_live_listing_fixture_is_posts_only_without_author_footer():
    from pathlib import Path

    from radar.sources import parse_reddit_feed

    fixture = Path(__file__).parent / "fixtures/reddit-listing-excerpt.xml"
    items, _ = parse_reddit_feed(fixture.read_bytes(), "reddit")
    assert len(items) == 2
    assert all(item.kind == "post" and item.author is None for item in items)
    assert "Really couldn't find any good existing quantization" in items[0].text
    assert "submitted by" not in items[0].text
    assert items[0].metadata["account_karma"] is None
    assert items[0].metadata["content_complete"] is False
    assert items[0].published_at == "2026-10-21T19:11:31Z"


def test_reddit_comment_schema_synthetic_does_not_invent_parent_or_karma():
    from radar.sources import parse_reddit_feed

    # Synthetic schema boundary only: live thread endpoints returned 429, no real comments obtained.
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Test</title>
    <entry><id>t1_abc</id><author><name>private_author</name></author>
    <title>private_author on story</title><link href="https://www.reddit.com/r/Test/comments/xyz/story/abc/"/>
    <updated>2026-10-21T10:00:00Z</updated><content type="html">&lt;div class="md"&gt;It costs 5.&lt;/div&gt; submitted by private_author</content></entry></feed>"""
    items, _ = parse_reddit_feed(atom, "reddit", "t3_xyz")
    assert len(items) == 1 and items[0].kind == "comment"
    assert items[0].text == "It costs 5."
    assert items[0].title == "" and items[0].parent_id is None
    assert items[0].metadata["root_id"] == "t3_xyz"
    assert items[0].metadata["reply_hierarchy_available"] is False
    assert "private_author" not in str(items[0].as_dict())


def test_reddit_stops_after_first_thread_429_and_keeps_posts_partial():
    from pathlib import Path

    from radar.sources import reddit_scan

    fixture = Path(__file__).parent / "fixtures/reddit-listing-excerpt.xml"
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if "/new/" in str(request.url):
            return httpx.Response(200, content=fixture.read_bytes())
        return httpx.Response(429, headers={"Retry-After": "3600"})

    with Fetcher({"www.reddit.com"}, transport=httpx.MockTransport(respond)) as fetcher:
        scan = reddit_scan(
            {"id": "reddit", "subreddit": "LocalLLaMA", "comment_threads": 2}, fetcher, {}, set()
        )
    assert len(calls) == 2
    assert scan.status == "partial" and not scan.complete and scan.retry_at
    assert scan.error == "http_429" and len(scan.items) == 2


def test_hn_samples_hot_threads_before_new_posts():
    from radar.sources import hn_scan

    class Fake:
        def json(self, url):
            if "newstories" in url:
                return [1]
            if "topstories" in url:
                return [2]
            native = int(url.rsplit("/", 1)[1].split(".")[0])
            return {
                1: {"title": "new", "kids": [11]},
                2: {"title": "hot", "kids": [22]},
                11: {"text": "new reply", "parent": 1},
                22: {"text": "hot reply", "parent": 2},
            }[native]

    scan = hn_scan({"id": "hn", "comment_threads": 1}, Fake(), {}, set())
    assert next(i for i in scan.items if i.kind == "comment").metadata["root_id"] == "2"


def test_ptt_bounded_batch_prioritizes_latest_and_exposes_partial():
    from radar.sources import ptt_scan

    class Fake:
        def get(self, url):
            if "index" in url:
                return (
                    b'<div class="r-list-container">'
                    + b"".join(
                        f'<div class="r-ent"><div class="title"><a href="/bbs/Test/M.{n}.A.html">{n}</a></div></div>'.encode()
                        for n in [100, 200, 300]
                    )
                    + b"</div>",
                    {},
                    200,
                )
            return b'<div id="main-content">body</div>', {}, 200

    cfg = {"id": "ptt", "url": "https://www.ptt.cc/bbs/Test/index.html", "pages": 1, "articles": 1}
    scan = ptt_scan(cfg, Fake(), {}, set())
    assert scan.items[0].native_id == "M.300.A.html"
    assert not scan.complete and scan.status == "partial"
    scan = ptt_scan(cfg, Fake(), {}, {"M.300.A.html"})
    assert scan.items[0].native_id == "M.200.A.html"


@pytest.mark.parametrize("status,code", [(403, "http_403"), (429, "http_429"), (401, "http_401")])
def test_http_errors_are_not_empty_success(status, code):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text="blocked"))
    with Fetcher({"example.com"}, transport=transport) as f:
        with pytest.raises(FetchError) as e:
            f.get("https://example.com/feed")
        assert e.value.code == code


def test_budget_prevents_extra_request():
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(200, text="ok")

    with Fetcher({"example.com"}, max_requests=1, transport=httpx.MockTransport(respond)) as f:
        f.get("https://example.com/a")
        with pytest.raises(FetchError, match="request_budget"):
            f.get("https://example.com/b")
    assert len(calls) == 1


def test_cross_host_redirect_cannot_fetch_internal_endpoint():
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    with (
        Fetcher({"example.com"}, transport=httpx.MockTransport(respond)) as f,
        pytest.raises(FetchError, match="unsafe_url"),
    ):
        f.get("https://example.com/a")
    assert len(calls) == 1


def test_oversized_response_rejected():
    with (
        Fetcher(
            {"example.com"},
            max_bytes=8,
            transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"123456789")),
        ) as f,
        pytest.raises(FetchError, match="response_too_large"),
    ):
        f.get("https://example.com/a")


def test_atom_updated_is_not_invented_publication_time():
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Test</title>
      <entry><id>x</id><title>Entry</title><updated>2026-10-21T10:00:00Z</updated>
      <link href="https://example.com/x"/><content type="html">Useful &lt;b&gt;body&lt;/b&gt;</content>
      </entry></feed>"""
    items, _warnings = parse_feed(atom, "test")
    assert items[0].published_at is None
    assert items[0].source_updated_at == "2026-10-21T10:00:00Z"
    assert items[0].text == "Useful body"


def test_login_page_is_not_empty_feed():
    with pytest.raises(FetchError, match="not_a_feed"):
        parse_feed(b"<html><form>sign in</form></html>", "test")


def test_ptt_comment_authors_are_not_persisted_and_body_excludes_pushes():
    page = """<div id="main-content"><div class="article-metaline"><span class="article-meta-tag">時間</span>
    <span class="article-meta-value">Wed Oct 21 14:39:07 2026</span></div>
    Actual post body<div class="push"><span class="push-tag">推</span>
    <span class="push-userid">private_handle</span><span class="push-content">: works well</span>
    <span class="push-ipdatetime">10/21 14:40</span></div></div>"""
    items = parse_ptt_article(page, "ptt", "https://www.ptt.cc/bbs/Soft_Job/M.1.html", "Title")
    assert items[0].published_at == "2026-10-21T06:39:07Z"
    assert "works well" not in items[0].text
    assert items[1].kind == "comment" and items[1].author is None
    assert "private_handle" not in str([i.as_dict() for i in items])


def test_lobsters_keeps_nested_parent_and_marks_sampled_comments():
    from radar.sources import lobsters_scan

    story = {
        "short_id": "story1",
        "title": "Compiler",
        "url": "https://example.com/article",
        "created_at": "2026-10-21T10:00:00Z",
        "comment_count": 3,
        "score": 5,
    }
    comments = [
        {
            "short_id": str(n),
            "comment_plain": "body",
            "parent_comment": "1" if n == 2 else None,
            "created_at": "2026-10-21T11:00:00Z",
            "commenting_user": "private_handle",
        }
        for n in [1, 2, 3]
    ]

    class Fake:
        def json(self, url):
            return {**story, "comments": comments} if "/s/" in url else [story]

    scan = lobsters_scan(
        {"id": "lobsters", "comment_threads": 1, "comments_per_thread": 2}, Fake(), {}, set()
    )
    assert scan.complete
    assert scan.items[0].metadata["external_url"] == story["url"]
    assert scan.items[2].parent_id == "1"
    assert scan.items[2].metadata["root_id"] == "story1"
    assert "sampled_comments:story1:2/3" in scan.warnings
    assert "private_handle" not in str([i.as_dict() for i in scan.items])


def test_hf_uses_raw_first_comment_as_body_and_hides_hidden_comment():
    from radar.sources import hf_scan

    listing = {
        "discussions": [
            {
                "num": 4,
                "title": "Memory",
                "numComments": 3,
                "createdAt": "2026-10-21T10:00:00Z",
                "isPullRequest": False,
            }
        ],
        "count": 1,
    }
    events = [
        {
            "id": str(n),
            "type": "comment",
            "createdAt": "2026-10-21T10:00:00Z",
            "author": {"name": "private_handle"},
            "data": {"hidden": n == 2, "latest": {"raw": f"memory {n} < 10 GB"}},
        }
        for n in range(3)
    ]

    class Fake:
        def json(self, url):
            return (
                listing if "?" in url else {**listing["discussions"][0], "events": events, "status": "open"}
            )

    scan = hf_scan({"id": "hf", "repo": "Qwen/Qwen3-8B"}, Fake(), {}, set())
    assert scan.complete
    assert scan.items[0].text == "memory 0 < 10 GB"
    assert scan.items[1].parent_id == "4"
    assert len(scan.items) == 2
    assert "private_handle" not in str([i.as_dict() for i in scan.items])


def test_v2ex_stale_cache_is_not_no_new_or_fresh_complete():
    from radar.sources import v2ex_scan

    class Fake:
        def get(self, url):
            return b"[]", {"age": "7200"}, 200

    scan = v2ex_scan({"id": "v2ex", "max_cache_age_seconds": 1800}, Fake(), {}, set())
    assert scan.status == "stale" and not scan.complete
    assert "upstream_cache_age:7200" in scan.warnings


def test_adapters_propagate_backoff_after_partial_items():
    from radar.sources import hn_scan

    class Fake:
        def json(self, url):
            if "stories" in url:
                return [1, 2]
            if "item/1" in url:
                return {"title": "first"}
            raise FetchError("http_429", "rate_limited", 600)

    scan = hn_scan({"id": "hn"}, Fake(), {}, set())
    assert scan.items and scan.status == "partial" and scan.retry_at.endswith("Z")


def test_hn_missing_story_does_not_advance_checkpoint_and_deleted_reply_is_tombstone():
    from radar.sources import hn_scan

    class Fake:
        def json(self, url):
            if "stories" in url:
                return [1, 2]
            if "item/1." in url:
                return {"title": "story", "kids": [3]}
            if "item/3." in url:
                return {"deleted": True, "parent": 1}
            return None

    scan = hn_scan({"id": "hn"}, Fake(), {}, set())
    assert not scan.complete
    deleted = next(i for i in scan.items if i.native_id == "3")
    assert deleted.kind == "tombstone" and deleted.text == ""
    assert deleted.metadata["root_id"] == "1"


def test_ptt_refreshes_known_articles_while_discovering_new_posts():
    from radar.sources import ptt_scan

    class Fake:
        def get(self, url):
            if "index" in url:
                return (
                    b'<div class="r-list-container">'
                    + b"".join(
                        f'<div class="r-ent"><div class="title"><a href="/bbs/Test/M.{n}.A.html">{n}</a></div></div>'.encode()
                        for n in [100, 200, 300]
                    )
                    + b"</div>",
                    {},
                    200,
                )
            return b'<div id="main-content">body</div>', {}, 200

    scan = ptt_scan(
        {
            "id": "ptt",
            "url": "https://www.ptt.cc/bbs/Test/index.html",
            "pages": 1,
            "articles": 2,
            "refresh_articles": 1,
        },
        Fake(),
        {},
        {"M.300.A.html"},
    )
    assert {i.native_id for i in scan.items} == {"M.300.A.html", "M.200.A.html"}
    assert not scan.complete
