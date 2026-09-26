"""A read-only, loopback-only browser for NAS directory metadata."""

import hmac
import html
import posixpath
import secrets
import sys
import threading
import webbrowser
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from string import Template
from typing import Any, Dict, Mapping, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from .client import ApiClient, _human_bytes, list_remote_page
from .errors import AuthenticationError, Nass3cpError


PAGE_SIZE = 100
_KINDS = {"directory": "文件夹", "file": "文件", "symlink": "符号链接", "other": "其他"}


def _escape(value: Any) -> str:
    # Preserve a legible representation of Unix surrogate-escaped filenames.
    return html.escape(str(value).encode("utf-8", "backslashreplace").decode("utf-8"), quote=True)


def _url(path: str, cursor: int = 0) -> str:
    return "/?" + urlencode({"path": path, "cursor": cursor}, errors="surrogatepass")


def _link(label: str, path: str, cursor: int = 0, css: str = "button") -> str:
    return '<a class="%s" href="%s">%s</a>' % (css, _escape(_url(path, cursor)), _escape(label))


def _timestamp(value: Optional[int]) -> str:
    if value is None:
        return '<span class="unavailable">不可用</span>'
    try:
        date = datetime.fromtimestamp(value / 1_000_000_000).astimezone()
        return '<time datetime="%s">%s</time>' % (
            date.isoformat(), date.strftime("%Y-%m-%d %H:%M:%S"),
        )
    except (OSError, OverflowError, ValueError):
        return '<span class="unavailable">不可用</span>'


def _render(
    server: "BrowserServer", path: str, cursor: int,
    page: Optional[Mapping[str, Any]] = None, error: Optional[str] = None,
) -> bytes:
    page = page or {}
    current = page.get("resolved_path") or path
    # Older NAS services do not yet return canonical navigation metadata.
    parent = page.get("parent_path")
    if "parent_path" not in page and current not in (".", "/"):
        parent = posixpath.dirname(current.rstrip("/")) or "."
    navigation = _link("起始目录", server.initial_path)
    navigation += (
        _link("↑ 上一级", parent) if parent is not None
        else '<span class="button disabled" aria-disabled="true">↑ 上一级</span>'
    )
    navigation += _link("刷新", current, cursor)
    roots = "".join(_link(root, root, css="root") for root in page.get("roots", []))
    if roots:
        roots = '<nav class="roots" aria-label="共享目录"><span>共享目录</span>%s</nav>' % roots

    rows = []
    for entry in page.get("entries", []):
        name = entry["name"]
        kind = entry["type"]
        name_html = '<bdi>%s</bdi>' % _escape(name)
        if kind == "directory":
            name_html = '<a href="%s">%s<span aria-hidden="true"> /</span></a>' % (
                _escape(_url(posixpath.join(current, name))), name_html,
            )
        size = entry["size"]
        size_html = (
            '<span title="%s 字节">%s</span>' % (format(size, ","), _human_bytes(size))
            if size is not None else '<span class="unavailable">—</span>'
        )
        rows.append(
            '<tr><td class="name"><span class="icon %s" aria-hidden="true"></span>%s</td>'
            '<td>%s</td><td class="size">%s</td><td>%s</td><td>%s</td></tr>'
            % (kind, name_html, _KINDS[kind], size_html,
               _timestamp(entry.get("birthtime_ns")), _timestamp(entry["mtime_ns"]))
        )
    if error is not None:
        content = '<div class="message error" role="alert"><h2>无法读取目录</h2><p>%s</p><p>请检查路径或 NAS 连接，然后重试。</p></div>' % _escape(error)
    else:
        if not rows:
            label = "此文件夹为空" if cursor == 0 else "此页没有文件，请返回首页或刷新目录。"
            rows.append('<tr><td colspan="5" class="empty">%s</td></tr>' % label)
        content = (
            '<div class="table-scroll"><table><caption class="sr-only">NAS 文件列表</caption>'
            '<thead><tr><th scope="col">名称</th><th scope="col">类型</th>'
            '<th scope="col" class="size">大小</th><th scope="col">创建时间</th>'
            '<th scope="col">修改时间</th></tr></thead><tbody>%s</tbody></table></div>'
        ) % "".join(rows)

    total = page.get("total")
    count = len(page.get("entries", []))
    summary = "共 %s 项" % format(total, ",") if total is not None else "本页 %d 项" % count
    if count:
        summary += " · 显示 %d–%d 项" % (cursor + 1, cursor + count)
    pagination = ""
    if cursor:
        pagination += _link("首页", current)
        pagination += _link("上一页", current, max(0, cursor - PAGE_SIZE))
    if page.get("next_cursor") is not None:
        pagination += _link("下一页", current, page["next_cursor"])
    return server.template.substitute(
        endpoint=_escape(server.api.base_url), path=_escape("nas:" + current),
        navigation=navigation, roots=roots, content=content,
        summary=_escape(summary) if error is None else "读取失败",
        pagination=pagination,
    ).encode("utf-8")


class BrowserServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, api: ApiClient, initial_path: str, port: int = 8765):
        self.api = api
        self.initial_path = initial_path
        self.launch_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.api_lock = threading.Lock()
        self.template = Template(resources.read_text("nass3cp", "browse.html", encoding="utf-8"))
        self.stylesheet = resources.read_binary("nass3cp", "browse.css")
        super().__init__(("127.0.0.1", port), BrowserHandler)
        self.cookie_name = "nass3cp_browse_%d" % self.server_port

    @property
    def url(self) -> str:
        return "http://localhost:%d/?token=%s" % (self.server_port, self.launch_token)


class BrowserHandler(BaseHTTPRequestHandler):
    server_version = "nass3cp-browser"
    sys_version = ""

    @property
    def browser(self) -> BrowserServer:
        return self.server  # type: ignore[return-value]

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format_string: str, *args: Any) -> None:
        # In particular, do not log the local launch token or NAS filenames.
        pass

    def _send(
        self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8",
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _message(self, status: int, message: str) -> None:
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _local_request(self) -> bool:
        host = self.headers.get("Host", "")
        allowed = {"localhost:%d" % self.server.server_port, "127.0.0.1:%d" % self.server.server_port}
        if host not in allowed or self.headers.get("Origin", "http://" + host) != "http://" + host:
            self._message(403, "仅允许从本机浏览地址访问。")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._message(403, "请直接打开命令行显示的本机浏览地址。")
            return False
        return True

    def _authenticated(self) -> bool:
        cookie: SimpleCookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        value = cookie.get(self.browser.cookie_name)
        return value is not None and hmac.compare_digest(
            value.value.encode("utf-8"), self.browser.session_token.encode("ascii")
        )

    def do_GET(self) -> None:
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # A closed tab must not interrupt the CLI session.

    def _get(self) -> None:
        if not self._local_request():
            return
        try:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True, errors="surrogatepass", max_num_fields=8)
            if any(len(values) != 1 for values in query.values()):
                raise ValueError("duplicate query parameters")
        except (ValueError, UnicodeError):
            self._message(400, "无效的浏览请求。")
            return
        if parsed.path == "/" and "token" in query:
            if not hmac.compare_digest(query["token"][0].encode("utf-8", "surrogatepass"), self.browser.launch_token.encode("ascii")):
                self._message(403, "浏览会话无效，请使用命令行显示的完整地址。")
                return
            self._send(303, b"", headers={
                "Location": _url(self.browser.initial_path),
                "Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (
                    self.browser.cookie_name, self.browser.session_token,
                ),
            })
            return
        if not self._authenticated():
            self._message(403, "请使用命令行显示的完整地址打开文件浏览器。")
            return
        if parsed.path == "/browse.css":
            self._send(200, self.browser.stylesheet, "text/css; charset=utf-8")
            return
        if parsed.path != "/":
            self._message(404, "页面不存在。")
            return
        path = query.get("path", [self.browser.initial_path])[0]
        if path.startswith("nas:"):
            path = path[4:]
        path = path or "."
        try:
            cursor = int(query.get("cursor", ["0"])[0])
            if cursor < 0 or cursor > sys.maxsize or "\x00" in path:
                raise ValueError("invalid path or cursor")
        except ValueError:
            self._message(400, "无效的目录或页码。")
            return
        try:
            with self.browser.api_lock:
                page = list_remote_page(self.browser.api, path, cursor, PAGE_SIZE)
            self._send(200, _render(self.browser, path, cursor, page))
        except AuthenticationError:
            self._send(502, _render(self.browser, path, cursor, error="NAS 密码已失效，请在命令行重新连接。"))
        except (Nass3cpError, OSError, ValueError) as exc:
            self._send(502, _render(self.browser, path, cursor, error=str(exc)))


def run_browser(api: ApiClient, path: str, port: int = 8765, open_browser: bool = True) -> None:
    # Authenticate and validate the starting directory before listening locally.
    list_remote_page(api, path, page_size=PAGE_SIZE)
    with BrowserServer(api, path, port) as server:
        print("NAS file browser: %s" % server.url, flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        if open_browser:
            try:
                opened = webbrowser.open(server.url, new=2)
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                print("Open the URL above in your browser.", file=sys.stderr)
        server.serve_forever(poll_interval=0.2)
