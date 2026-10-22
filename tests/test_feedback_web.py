import io
import re
from urllib.parse import urlencode

from radar.delivery import enqueue, initialize
from radar.feedback_web import FeedbackApp, TailscaleIdentity
from radar.store import Store


def request(app, method="GET", body="", **overrides):
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": "/reports/r1",
        "REMOTE_USER": "owner",
        "HTTP_HOST": "radar.example",
        "CONTENT_LENGTH": str(len(body.encode())),
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
        "wsgi.input": io.BytesIO(body.encode()),
    }
    env.update(overrides)
    result = {}

    def start(status, headers):
        result["status"] = int(status.split()[0])
        result["headers"] = dict(headers)

    result["body"] = b"".join(app(env, start)).decode()
    return result


def test_get_is_readonly_xss_is_escaped_and_post_is_idempotent(tmp_path):
    with Store(tmp_path) as db:
        initialize(db)
        enqueue(db, "r1", 1, "test", {"text": "report"})
    report = {
        "report_id": "r1",
        "cutoff": "2026-10-22",
        "cards": [
            {
                "title": "<script>bad()</script>",
                "claims": [{"text": "<img src=x onerror=bad()>", "evidence": []}],
                "limitations": [],
            }
        ],
    }
    app = FeedbackApp(tmp_path, lambda rid: report, "https://radar.example", "owner", b"x" * 32)
    page = request(app)
    assert page["status"] == 200 and "<script>" not in page["body"]
    with Store(tmp_path) as db:
        assert db.conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 0
    token = re.search(r'name="csrf" value="([^"]+)"', page["body"])[1]
    form = urlencode({"target": "r1", "label": "useful", "csrf": token})
    for _ in range(2):
        result = request(app, "POST", form, PATH_INFO="/feedback", HTTP_ORIGIN="https://radar.example")
        assert result["status"] == 200
    with Store(tmp_path) as db:
        assert db.conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 1


def test_identity_origin_and_csrf_cannot_be_forged(tmp_path):
    app = FeedbackApp(tmp_path, lambda rid: {}, "https://radar.example", "owner", b"x" * 32)
    assert request(app, REMOTE_USER="attacker")["status"] == 401
    assert request(app, REMOTE_USER="", HTTP_TAILSCALE_USER_LOGIN="owner")["status"] == 401
    assert (
        request(
            app,
            "POST",
            "target=r1&label=useful&csrf=fake",
            PATH_INFO="/feedback",
            HTTP_ORIGIN="https://evil.example",
        )["status"]
        == 403
    )
    assert (
        request(
            app,
            "POST",
            "target=r1&label=useful&csrf=fake",
            PATH_INFO="/feedback",
            HTTP_ORIGIN="https://radar.example",
        )["status"]
        == 403
    )


def test_identity_adapter_rejects_direct_private_network_headers(tmp_path):
    import pytest

    app = FeedbackApp(
        tmp_path, lambda rid: {"report_id": rid, "cards": []}, "https://radar.example", "owner", b"x" * 32
    )
    with pytest.raises(ValueError, match="identity_listener_must_be_loopback"):
        TailscaleIdentity(app, listen_address="0.0.0.0")
    proxy = TailscaleIdentity(app, listen_address="127.0.0.1")
    for remote in ("192.0.2.10", "192.168.1.1", "bad"):
        assert request(proxy, REMOTE_ADDR=remote, HTTP_TAILSCALE_USER_LOGIN="owner")["status"] == 401
    assert request(proxy, REMOTE_ADDR="127.0.0.1", HTTP_TAILSCALE_USER_LOGIN="owner")["status"] == 200
    assert request(proxy, REMOTE_ADDR="127.0.0.1", HTTP_TAILSCALE_USER_LOGIN="another")["status"] == 401
