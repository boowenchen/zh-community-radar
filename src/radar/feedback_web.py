"""Private WSGI report/feedback surface. Authentication must be established by a trusted adapter.

REMOTE_USER is a WSGI identity, never copied from arbitrary HTTP headers here.
This module does not create a public listener, credentials, or a Tailscale route.
"""

import hashlib
import hmac
import html
import ipaddress
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import portalocker

from . import delivery
from .store import Store

STYLE = "body{max-width:760px;margin:2rem auto;padding:0 1rem;font:17px/1.7 system-ui;color:#233045;background:#fafaf7}article{background:white;padding:1.2rem;border:1px solid #ddd;border-radius:12px;margin:1rem 0}h1,h2{line-height:1.3}a{color:#175c87}button{font:inherit;padding:.55rem 1rem;margin:.25rem;border:1px solid #7995a8;border-radius:6px;background:#eff7fc}small{color:#596574}"


class TailscaleIdentity:
    """Use only behind Serve, on a loopback-only listener. Local processes remain trusted.

    Serve strips incoming identity headers and supplies its own. This does not create
    a route or prove configuration; a direct LAN/VPN listener is forbidden.
    """

    def __init__(self, app, *, listen_address):
        if not ipaddress.ip_address(listen_address).is_loopback:
            raise ValueError("identity_listener_must_be_loopback")
        self.app = app

    def __call__(self, env, start):
        clean = env.copy()
        clean.pop("REMOTE_USER", None)
        try:
            loopback = ipaddress.ip_address(env.get("REMOTE_ADDR", "")).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            identity = env.get("HTTP_TAILSCALE_USER_LOGIN", "")
            if identity and len(identity) <= 254 and not any(c in identity for c in "\r\n,"):
                clean["REMOTE_USER"] = identity
        return self.app(clean, start)


def escape(text):
    return html.escape(str(text), quote=True)


def render_report(report, csrf):
    parts = [
        '<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>社群雷達</title><style>" + STYLE + "</style><h1>社群雷達</h1>",
        "<p>資料截至 " + escape(report.get("cutoff", "未知")) + "</p>",
    ]
    if report.get("independent_review") != "pass":
        parts.append("<p><strong>驗證中預覽</strong>：尚未完成獨立內容外審。</p>")
    for card in report.get("cards", []):
        parts.append("<article><h2>" + escape(card["title"]) + "</h2>")
        for claim in card.get("claims", []):
            parts.append("<p>" + escape(claim["text"]) + "</p><p>")
            for ref in claim.get("evidence", []):
                url = ref.get("url", "")
                parsed = urlsplit(url)
                if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username:
                    parts.append(
                        '<a target="_blank" rel="noreferrer noopener" href="'
                        + escape(url)
                        + '">閱讀原文</a> '
                    )
            parts.append("</p>")
        for reaction in card.get("reactions", []):
            parts.append("<p>樣本觀點：" + escape(reaction.get("reason", "")) + "</p>")
        parts.append("<small>描述僅限取得的樣本，不代表整個社群。</small></article>")
    if not report.get("cards"):
        parts.append("<p>這個時間窗尚無通過篩選的內容。</p>")
    parts += [
        '<form method="post" action="/feedback"><input type="hidden" name="target" value="'
        + escape(report["report_id"])
        + '">',
        '<input type="hidden" name="csrf" value="' + escape(csrf) + '"><p>這份日報對你有幫助嗎？</p>',
    ]
    for value, label in [("useful", "有用"), ("known", "已知"), ("too_broad", "太泛")]:
        parts.append('<button name="label" value="' + value + '">' + label + "</button>")
    parts.append("</form></html>")
    return "".join(parts)


class FeedbackApp:
    def __init__(self, data_dir, report_reader, origin, identity, secret, now=time.time):
        if len(secret) < 32 or urlsplit(origin).scheme != "https" or not identity:
            raise ValueError("feedback_config_invalid")
        self.data_dir, self.reader, self.origin, self.identity, self.secret, self.now = (
            data_dir,
            report_reader,
            origin,
            identity,
            secret,
            now,
        )

    def token(self, target):
        payload = f"{target}:{self.identity}:{int(self.now()) + 3600}:{secrets.token_hex(12)}"
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return payload + "|" + signature

    def valid_token(self, token, target):
        try:
            payload, signature = token.rsplit("|", 1)
            item, identity, expiry, _ = payload.split(":")
            expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
            return (
                item == target
                and identity == self.identity
                and int(expiry) >= self.now()
                and hmac.compare_digest(signature, expected)
            )
        except (ValueError, TypeError):
            return False

    def __call__(self, env, start):
        def respond(status, body):
            headers = [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
                (
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                ),
            ]
            start(status, headers)
            return [body.encode()]

        if env.get("REMOTE_USER") != self.identity:
            return respond("401 Unauthorized", "需要已驗證的私人存取身分。")
        if env.get("HTTP_HOST") != urlsplit(self.origin).netloc:
            return respond("403 Forbidden", "入口不符。")
        path, method = env.get("PATH_INFO", ""), env.get("REQUEST_METHOD", "GET")
        if method == "GET" and re.fullmatch(r"/reports/[A-Za-z0-9_-]{1,80}", path):
            rid = path.rsplit("/", 1)[-1]
            try:
                report = self.reader(rid)
                if not report or report["report_id"] != rid:
                    raise ValueError("report_not_found")
                return respond("200 OK", render_report(report, self.token(rid)))
            except (ValueError, KeyError):
                return respond("404 Not Found", "沒有這份日報。")
        if method != "POST" or path != "/feedback":
            return respond("404 Not Found", "找不到此頁。")
        if env.get("HTTP_ORIGIN") != self.origin:
            return respond("403 Forbidden", "來源驗證失敗。")
        try:
            size = int(env.get("CONTENT_LENGTH", "0"))
            if (
                not 0 < size <= 4096
                or env.get("CONTENT_TYPE", "").split(";")[0] != "application/x-www-form-urlencoded"
            ):
                return respond("400 Bad Request", "表單格式錯誤。")
            form = parse_qs(
                env["wsgi.input"].read(size).decode("utf-8"), strict_parsing=True, max_num_fields=3
            )
            if set(form) != {"target", "label", "csrf"} or any(len(v) != 1 for v in form.values()):
                return respond("400 Bad Request", "表單格式錯誤。")
            target, label, csrf = (form[k][0] for k in ("target", "label", "csrf"))
            if not self.valid_token(csrf, target):
                return respond("403 Forbidden", "表單已失效，請重新開啟日報。")
            with portalocker.Lock(str(self.data_dir) + "/writer.lock", timeout=0), Store(self.data_dir) as db:
                delivery.initialize(db)
                receipt = delivery.add_feedback(
                    db, target, label, self.identity, "user", hashlib.sha256(csrf.encode()).hexdigest()
                )
            return respond(
                "200 OK",
                '<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><p>已記錄你的回饋，謝謝。</p><small>收據 '
                + escape(receipt["feedback_id"])
                + "</small></html>",
            )
        except portalocker.exceptions.LockException:
            return respond("503 Service Unavailable", "正在更新，請稍後再試。")
        except (ValueError, UnicodeDecodeError, KeyError):
            return respond("400 Bad Request", "無法接受這份回饋。")
