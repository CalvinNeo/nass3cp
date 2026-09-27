"""A loopback-only NAS browser with verified, per-file transfers."""

import hmac
import html
import json
import posixpath
import re
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
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .client import ApiClient, _human_bytes, list_remote_page
from .downloads import DownloadError, DownloadManager
from .errors import AuthenticationError, DownloadCancelled, Nass3cpError
from .uploads import UploadError, UploadManager


PAGE_SIZE = 100
_KINDS = {"directory": "Folder", "file": "File", "symlink": "Symbolic link", "other": "Other"}


def _escape(value: Any) -> str:
    # Preserve a legible representation of Unix surrogate-escaped filenames.
    return html.escape(str(value).encode("utf-8", "backslashreplace").decode("utf-8"), quote=True)


def _url(path: str, cursor: int = 0) -> str:
    return "/?" + urlencode({"path": path, "cursor": cursor}, errors="surrogatepass")


def _link(label: str, path: str, cursor: int = 0, css: str = "button") -> str:
    return '<a class="%s" href="%s">%s</a>' % (css, _escape(_url(path, cursor)), _escape(label))


def _icon(name: str) -> str:
    paths = {
        "home": "M3 10 12 3l9 7M5 9v12h5v-7h4v7h5V9",
        "folder": "M3 7V5h6l2 3h10v12H3V7Z",
        "up": "M12 20V4m-6 6 6-6 6 6",
        "refresh": "M20 11a8 8 0 1 0-2 6M20 4v7h-7",
    }
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="%s"/></svg>' % paths[name]


def _navigation_link(label: str, path: Optional[str], icon: str, cursor: int = 0) -> str:
    content = _icon(icon) + '<span class="sr-only">%s</span>' % _escape(label)
    if path is None:
        return '<span class="tool-button disabled" aria-disabled="true" title="%s">%s</span>' % (
            _escape(label), content,
        )
    return '<a class="tool-button" href="%s" title="%s">%s</a>' % (
        _escape(_url(path, cursor)), _escape(label), content,
    )


def _timestamp(value: Optional[int]) -> str:
    if value is None:
        return '<span class="unavailable">N/A</span>'
    try:
        date = datetime.fromtimestamp(value / 1_000_000_000).astimezone()
        return '<time datetime="%s">%s</time>' % (
            date.isoformat(), date.strftime("%Y-%m-%d %H:%M:%S"),
        )
    except (OSError, OverflowError, ValueError):
        return '<span class="unavailable">N/A</span>'


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
    navigation = _navigation_link("Up", parent, "up")
    navigation += _navigation_link("Refresh", current, "refresh", cursor)
    start_folder = '<a class="sidebar-link" href="%s">%s<span>Start folder</span></a>' % (
        _escape(_url(server.initial_path)), _icon("home"),
    )
    shared_roots = page.get("roots", [])
    active_root = max(
        (root for root in shared_roots if current == root or current.startswith(root.rstrip("/") + "/")),
        key=len, default=None,
    )
    roots = "".join(
        '<a class="sidebar-link%s" href="%s" title="%s"%s>%s<span>%s</span></a>' % (
            " active" if root == active_root else "", _escape(_url(root)), _escape(root),
            ' aria-current="location"' if root == active_root else "", _icon("folder"),
            _escape(posixpath.basename(root.rstrip("/")) or root),
        ) for root in shared_roots
    )
    if roots:
        roots = '<nav class="roots" aria-label="Shared folders"><p class="sidebar-label">Shared folders</p>%s</nav>' % roots

    rows = []
    for entry in page.get("entries", []):
        name = entry["name"]
        kind = entry["type"]
        name_html = '<bdi>%s</bdi>' % _escape(name)
        selection = '<span class="selection-spacer" aria-hidden="true"></span>'
        if kind == "file":
            selection = '<input type="checkbox" class="file-select" data-file-path="%s" aria-label="%s">' % (
                _escape(json.dumps(posixpath.join(current, name), ensure_ascii=True)),
                _escape("Select " + name),
            )
        if kind == "directory":
            name_html = '<a href="%s">%s</a>' % (
                _escape(_url(posixpath.join(current, name))), name_html,
            )
        size = entry["size"]
        size_html = (
            '<span title="%s bytes">%s</span>' % (format(size, ","), _human_bytes(size))
            if size is not None else '<span class="unavailable">—</span>'
        )
        rows.append(
            '<tr><td class="name">%s<span class="icon %s" aria-hidden="true"></span>%s</td>'
            '<td>%s</td><td class="size">%s</td><td class="created">%s</td><td>%s</td></tr>'
            % (selection, kind, name_html, _KINDS[kind], size_html,
               _timestamp(entry.get("birthtime_ns")), _timestamp(entry["mtime_ns"]))
        )
    if error is not None:
        content = (
            '<div class="message error" role="alert"><h2>Unable to read folder</h2>'
            '<p>%s</p><p>Check the path or NAS connection, then try again.</p></div>'
        ) % _escape(error)
    else:
        if not rows:
            label = "This folder is empty" if cursor == 0 else "No files on this page. Go to the first page or refresh the folder."
            rows.append('<tr><td colspan="5" class="empty">%s</td></tr>' % label)
        content = (
            '<div class="table-scroll"><table><caption class="sr-only">NAS file list</caption>'
            '<thead><tr><th scope="col"><input type="checkbox" class="select-all" aria-label="Select visible files">Name</th><th scope="col">Type</th>'
            '<th scope="col" class="size">Size</th><th scope="col" class="created">Created</th>'
            '<th scope="col">Modified</th></tr></thead><tbody>%s</tbody></table></div>'
        ) % "".join(rows)

    total = page.get("total")
    count = len(page.get("entries", []))
    summary = "Total: %s" % format(total, ",") if total is not None else "Items on this page: %d" % count
    if count:
        summary += " · Showing %d–%d" % (cursor + 1, cursor + count)
    pagination = ""
    if cursor:
        pagination += _link("First page", current)
        pagination += _link("Previous", current, max(0, cursor - PAGE_SIZE))
    if page.get("next_cursor") is not None:
        pagination += _link("Next", current, page["next_cursor"])
    return server.template.substitute(
        endpoint=_escape(server.api.base_url), path=_escape("nas:" + current),
        folder_name=_escape(posixpath.basename(current.rstrip("/")) if current not in (".", "/") else "Files"),
        start_folder=start_folder,
        navigation=navigation, roots=roots, content=content,
        summary=_escape(summary) if error is None else "Could not load folder",
        pagination=pagination,
        upload_disabled="disabled" if error is not None else "",
    ).encode("utf-8")


class BrowserServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, api: ApiClient, initial_path: str, port: int = 8765,
                 download_concurrency: int = 1, jobs: int = 2, inflight: int = 3,
                 transfer_timeout: int = 86400):
        self.api = api
        self.initial_path = initial_path
        self.launch_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.api_lock = threading.Lock()
        self.template = Template(resources.read_text("nass3cp", "browse.html", encoding="utf-8"))
        self.stylesheet = resources.read_binary("nass3cp", "browse.css")
        self.search_script = resources.read_binary("nass3cp", "browse.js")
        self.download_script = resources.read_binary("nass3cp", "downloads.js")
        self.upload_script = resources.read_binary("nass3cp", "uploads.js")
        self.downloads = DownloadManager(api.clone, download_concurrency, jobs, inflight, transfer_timeout)
        self.uploads = UploadManager(api.clone, jobs, inflight, transfer_timeout)
        self.search_ids = set()  # type: set
        self.active_search_ids = set()  # type: set
        super().__init__(("127.0.0.1", port), BrowserHandler)
        self.cookie_name = "nass3cp_browse_%d" % self.server_port

    @property
    def url(self) -> str:
        return "http://localhost:%d/?token=%s" % (self.server_port, self.launch_token)

    def server_close(self) -> None:
        try:
            self.uploads.stop()
            self.downloads.stop()
            with self.api_lock:
                for identifier in self.active_search_ids:
                    try:
                        self.api.cancel_search(identifier)
                    except (Nass3cpError, OSError):
                        pass
                self.search_ids.clear()
                self.active_search_ids.clear()
        finally:
            super().server_close()


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
        self._headers(status, len(body), content_type, headers)
        self.wfile.write(body)

    def _headers(self, status: int, length: int, content_type: str,
                 headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _download_file(self, identifier: str) -> None:
        item, handle = self.browser.downloads.open_file(identifier)
        complete = False
        try:
            with handle:
                fallback = "download" + posixpath.splitext(item.name)[1].encode("ascii", "ignore").decode("ascii")
                self._headers(200, item.total, "application/octet-stream", {
                    "Content-Disposition": 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (
                        fallback, quote(item.name, safe="")),
                    "Accept-Ranges": "none",
                })
                sent = 0
                while True:
                    if item.cancelled.is_set():
                        raise DownloadCancelled("Download cancelled")
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    self.wfile.write(block)
                    sent += len(block)
                    self.browser.downloads.sent_bytes(item, sent)
                self.wfile.flush()
                complete = sent == item.total
        except (DownloadCancelled, OSError):
            pass  # Closing a native browser download leaves a retryable save link.
        finally:
            self.browser.downloads.finish_sending(item, complete)

    def _message(self, status: int, message: str) -> None:
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _json(self, status: int, value: Mapping[str, Any]) -> None:
        self._send(status, json.dumps(value, ensure_ascii=True).encode("utf-8"), "application/json; charset=utf-8")

    def _search_error(self, error: Exception) -> None:
        cause = error.__cause__
        status = cause.code if isinstance(cause, HTTPError) else 502
        self._json(status, {"error": str(error)})

    def do_POST(self) -> None:
        if not self._local_request():
            return
        if not self._authenticated():
            self._message(403, "Open the full browser URL shown in the terminal.")
            return
        # Custom headers and same-origin checks protect all actions.
        is_download = self.path.startswith("/api/downloads")
        is_upload = self.path.startswith("/api/uploads")
        action = "upload" if is_upload else "download" if is_download else "search"
        if (self.headers.get("X-Nass3cp-Request") != action
                or self.headers.get_content_type() != "application/json"):
            self._json(403, {"error": "Requests must come from this page."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (self.headers.get("Transfer-Encoding") is not None
                    or len(self.headers.get_all("Content-Length", [])) != 1
                    or not 1 <= length <= (2 * 1024 * 1024 if is_download or is_upload else 16384)):
                raise ValueError("invalid request length")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("invalid request body")
            if is_upload:
                if self.path == "/api/uploads":
                    self._json(202, self.browser.uploads.enqueue(body))
                else:
                    match = re.fullmatch(r"/api/uploads/([0-9a-f]{32})/cancel", self.path)
                    if not match:
                        raise UploadError(404, "Upload endpoint not found.")
                    self._json(200, self.browser.uploads.cancel(match.group(1)))
                return
            if is_download:
                if self.path == "/api/downloads":
                    self._json(202, self.browser.downloads.enqueue(body))
                else:
                    match = re.fullmatch(r"/api/downloads/([0-9a-f]{32})/cancel", self.path)
                    if not match:
                        raise DownloadError(404, "Download endpoint not found.")
                    self._json(200, self.browser.downloads.cancel(match.group(1)))
                return
            with self.browser.api_lock:
                if self.path == "/api/searches":
                    value = self.browser.api.start_search(
                        body.get("path", ""), body.get("pattern"),
                        body.get("regex", False), body.get("case_sensitive", False),
                    )
                    identifier = ApiClient._search_id(value.get("id"))
                    self.browser.search_ids.add(identifier)
                    # A successful start means the NAS-wide slot is free of older jobs.
                    self.browser.active_search_ids.clear()
                    self.browser.active_search_ids.add(identifier)
                    while len(self.browser.search_ids) > 8:
                        finished = self.browser.search_ids - self.browser.active_search_ids
                        if not finished:
                            break
                        self.browser.search_ids.remove(next(iter(finished)))
                else:
                    match = re.fullmatch(r"/api/searches/([0-9a-f]{32})/cancel", self.path)
                    if not match or match.group(1) not in self.browser.search_ids:
                        self._json(404, {"error": "Search does not belong to this browser session."})
                        return
                    value = self.browser.api.cancel_search(match.group(1))
                self._json(200, value)
        except (ValueError, UnicodeError):
            self._json(400, {"error": "Invalid request."})
        except (DownloadError, UploadError) as exc:
            self._json(exc.status, {"error": str(exc)})
        except (Nass3cpError, OSError) as exc:
            self._search_error(exc)

    def do_PUT(self) -> None:
        try:
            self._upload_file()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # An interrupted upload is cleaned up by the upload manager.

    def _upload_file(self) -> None:
        if not self._local_request():
            return
        if not self._authenticated():
            self._message(403, "Open the full browser URL shown in the terminal.")
            return
        if (self.headers.get("X-Nass3cp-Request") != "upload"
                or self.headers.get_content_type() != "application/octet-stream"):
            self._json(403, {"error": "Requests must come from this page."})
            return
        match = re.fullmatch(r"/api/uploads/([0-9a-f]{32})/file", self.path)
        if not match:
            self._json(404, {"error": "Upload endpoint not found."})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                    or not re.fullmatch(r"[0-9]{1,16}", lengths[0])):
                raise UploadError(400, "A single valid Content-Length is required.")
            value = self.browser.uploads.receive(match.group(1), self.rfile, int(lengths[0]))
            self._json(202, value)
        except UploadError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (Nass3cpError, OSError) as exc:
            self._search_error(exc)

    def _local_request(self) -> bool:
        host = self.headers.get("Host", "")
        allowed = {"localhost:%d" % self.server.server_port, "127.0.0.1:%d" % self.server.server_port}
        if host not in allowed or self.headers.get("Origin", "http://" + host) != "http://" + host:
            self._message(403, "Access is only allowed through the local browser address.")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._message(403, "Open the local URL shown in the terminal directly.")
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
            self._message(400, "Invalid browser request.")
            return
        if parsed.path == "/" and "token" in query:
            if not hmac.compare_digest(query["token"][0].encode("utf-8", "surrogatepass"), self.browser.launch_token.encode("ascii")):
                self._message(403, "Invalid browser session. Open the full URL shown in the terminal.")
                return
            self._send(303, b"", headers={
                "Location": _url(self.browser.initial_path),
                "Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (
                    self.browser.cookie_name, self.browser.session_token,
                ),
            })
            return
        if not self._authenticated():
            self._message(403, "Open the file browser using the full URL shown in the terminal.")
            return
        if parsed.path == "/browse.css":
            self._send(200, self.browser.stylesheet, "text/css; charset=utf-8")
            return
        if parsed.path == "/browse.js":
            self._send(200, self.browser.search_script, "text/javascript; charset=utf-8")
            return
        if parsed.path == "/downloads.js":
            self._send(200, self.browser.download_script, "text/javascript; charset=utf-8")
            return
        if parsed.path == "/uploads.js":
            self._send(200, self.browser.upload_script, "text/javascript; charset=utf-8")
            return
        if parsed.path == "/api/uploads":
            self._json(200, self.browser.uploads.state())
            return
        if parsed.path == "/api/downloads":
            self._json(200, self.browser.downloads.state())
            return
        match = re.fullmatch(r"/api/downloads/([0-9a-f]{32})/file", parsed.path)
        if match:
            try:
                self._download_file(match.group(1))
            except DownloadError as exc:
                self._json(exc.status, {"error": str(exc)})
            return
        match = re.fullmatch(r"/api/searches/([0-9a-f]{32})", parsed.path)
        if match:
            try:
                cursor = int(query.get("cursor", ["0"])[0])
                if cursor < 0:
                    raise ValueError("invalid cursor")
                with self.browser.api_lock:
                    if match.group(1) not in self.browser.search_ids:
                        self._json(404, {"error": "Search does not belong to this browser session."})
                        return
                    value = self.browser.api.search_state(match.group(1), cursor)
                    if value.get("status") not in ("starting", "running", "cancelling"):
                        self.browser.active_search_ids.discard(match.group(1))
                self._json(200, value)
            except ValueError:
                self._json(400, {"error": "Invalid search cursor."})
            except (Nass3cpError, OSError) as exc:
                self._search_error(exc)
            return
        if parsed.path != "/":
            self._message(404, "Page not found.")
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
            self._message(400, "Invalid folder path or page number.")
            return
        try:
            with self.browser.api_lock:
                page = list_remote_page(self.browser.api, path, cursor, PAGE_SIZE)
            self._send(200, _render(self.browser, path, cursor, page))
        except AuthenticationError:
            self._send(502, _render(self.browser, path, cursor, error="The NAS password is no longer valid. Reconnect from the terminal."))
        except (Nass3cpError, OSError, ValueError) as exc:
            self._send(502, _render(self.browser, path, cursor, error=str(exc)))


def run_browser(api: ApiClient, path: str, port: int = 8765, open_browser: bool = True,
                download_concurrency: int = 1, jobs: int = 2, inflight: int = 3,
                transfer_timeout: int = 86400) -> None:
    # Authenticate and validate the starting directory before listening locally.
    list_remote_page(api, path, page_size=PAGE_SIZE)
    with BrowserServer(api, path, port, download_concurrency, jobs, inflight, transfer_timeout) as server:
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
