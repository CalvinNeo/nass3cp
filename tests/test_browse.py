import http.client
import io
import json
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import Mock, patch

from nass3cp.browse import BrowserServer, PAGE_SIZE, run_browser
from nass3cp.errors import AuthenticationError, ProtocolError


def entry(name, kind="file", birthtime=None):
    return {
        "name": name, "type": kind, "size": 2048 if kind == "file" else None,
        "mtime_ns": 1_700_000_000_000_000_000, "birthtime_ns": birthtime,
    }


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock(base_url="https://nas.example:9443", password="private-nas-password")
        self.api.list_directory.return_value = {
            "entries": [entry("中文 & # + ?", "directory"), entry("movie.mp4"), entry("link", "symlink")],
            "next_cursor": None, "total": 3,
            "resolved_path": "/share", "parent_path": None, "roots": ["/share", "/other"],
        }
        self.server = BrowserServer(self.api, ".", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.cookie = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path="/", headers=None, method="GET", body=None):
        request_headers = {"Cookie": self.cookie}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            connection.close()

    def login(self):
        response = self.request("/?token=" + self.server.launch_token)
        self.assertEqual(response[0], 303)
        cookie = SimpleCookie(response[1]["Set-Cookie"])
        self.cookie = "%s=%s" % (self.server.cookie_name, cookie[self.server.cookie_name].value)
        return response

    def test_launch_requires_a_local_token_and_never_exposes_nas_password(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.request()[0], 403)
        self.assertEqual(self.request("/?token=incorrect")[0], 403)
        self.api.list_directory.assert_not_called()
        status, headers, body = self.login()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertNotIn("token=", headers["Location"])
        self.assertNotIn(self.api.password, str(headers) + body + self.server.url)
        status, headers, body = self.request(headers["Location"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(self.api.password, body)
        self.assertNotIn(self.server.session_token, body)

    def test_host_origin_and_cross_site_requests_are_rejected_without_reading_nas(self):
        self.login()
        for headers in (
            {"Host": "attacker.test"}, {"Host": "localhost:123"},
            {"Origin": "https://attacker.test"}, {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"}, {"Cookie": "invalid-cookie=value"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        self.api.list_directory.assert_not_called()

    def test_listing_escapes_names_and_links_and_shows_metadata(self):
        self.api.list_directory.return_value["entries"].extend([
            entry('<img src=x onerror="alert(1)">'),
            entry("created.txt", birthtime=1_600_000_000_000_000_000),
            entry("empty.txt"),
        ])
        self.api.list_directory.return_value["entries"][-1]["size"] = 0
        self.login()
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", body)
        self.assertNotIn("<img", body)
        expected_query = urlencode({"path": "/share/中文 & # + ?", "cursor": 0})
        self.assertIn(("/?" + expected_query).replace("&", "&amp;"), body)
        self.assertIn("Created", body)
        self.assertIn("2.0 KiB", body)
        self.assertIn("2,048 bytes", body)
        self.assertIn("0 B", body)
        self.assertIn("N/A", body)
        self.assertIn("2020-09", body)
        self.assertIn("Symbolic link", body)
        self.assertNotIn("path=%2Fshare%2Flink", body)
        self.assertIn('aria-disabled="true"', body)
        self.assertIn("path=%2Fother", body)
        self.api.list_directory.assert_called_once_with(".", 0, PAGE_SIZE)

    def test_directory_navigation_round_trips_unusual_names_and_windows_paths(self):
        self.login()
        path = "C:/share/中文 & # + ?/\udcff"
        query = urlencode({"path": path, "cursor": 0}, errors="surrogatepass")
        self.api.list_directory.return_value.update({
            "resolved_path": path, "parent_path": "C:/share", "entries": [], "total": 0,
        })
        status, _, body = self.request("/?" + query)
        self.assertEqual(status, 200)
        self.api.list_directory.assert_called_once_with(path, 0, PAGE_SIZE)
        self.assertIn("path=C%3A%2Fshare", body)
        self.assertIn("This folder is empty", body)
        self.assertIn("\\udcff", body)

    def test_pagination_fetches_only_the_requested_page(self):
        self.api.list_directory.return_value.update({
            "entries": [entry("%03d.txt" % i) for i in range(100, 200)],
            "next_cursor": 200, "total": 201,
        })
        self.login()
        status, _, body = self.request("/?path=nas%3A%2Fshare&cursor=100")
        self.assertEqual(status, 200)
        self.assertIn("Total: 201", body)
        self.assertIn("Showing 101–200", body)
        self.assertIn("cursor=200", body)
        self.assertIn("Previous", body)
        self.api.list_directory.assert_called_once_with("/share", 100, PAGE_SIZE)

    def test_legacy_directory_responses_work_without_new_metadata(self):
        self.api.list_directory.return_value = {
            "entries": [{"name": "file", "type": "file", "size": 1, "mtime_ns": 0}],
            "next_cursor": None,
        }
        self.login()
        status, _, body = self.request("/?path=folder")
        self.assertEqual(status, 200)
        self.assertIn("Items on this page: 1", body)
        self.assertIn("N/A", body)
        self.assertIn("path=.", body)

    def test_remote_errors_are_visible_and_escaped_and_refresh_can_recover(self):
        self.login()
        for error in (AuthenticationError("wrong"), ProtocolError("offline <script>")):
            self.api.list_directory.side_effect = error
            status, _, body = self.request()
            self.assertEqual(status, 502)
            self.assertIn("Unable to read folder", body)
            self.assertIn("Refresh", body)
            self.assertNotIn("<script>", body)
        self.api.list_directory.side_effect = None
        self.assertEqual(self.request()[0], 200)

    def test_invalid_requests_and_file_urls_cannot_invoke_nas_operations(self):
        self.login()
        for path in ("/?cursor=-1", "/?cursor=bad", "/?path=%00", "/?path=a&path=b", "/?path=%FF"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request("/etc/passwd")[0], 404)
        self.assertEqual(self.request("/v1/transfers/upload", method="POST")[0], 403)
        self.assertEqual(self.api.mock_calls, [])

    def test_stylesheet_is_served_without_reading_local_paths_or_nas(self):
        self.login()
        status, headers, body = self.request("/browse.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertIn("@media", body)
        self.api.list_directory.assert_not_called()

    def test_only_regular_files_can_be_selected_in_the_list(self):
        self.login()
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body.count('class="file-select"'), 1)
        self.assertIn('aria-label="Select movie.mp4"', body)
        self.assertIn('data-file-path="&quot;/share/movie.mp4&quot;"', body)
        self.assertIn("Download selected", body)
        self.assertIn('aria-label="Downloads"', body)

    def test_download_actions_and_files_require_the_local_session(self):
        headers = {"Content-Type": "application/json", "X-Nass3cp-Request": "download"}
        body = json.dumps({"paths": ["/share/movie.mp4"]})
        identifier = "a" * 32
        for path in ("/api/downloads", "/api/downloads/" + identifier + "/file"):
            self.assertEqual(self.request(path)[0], 403)
        self.assertEqual(self.request("/api/downloads", headers, "POST", body)[0], 403)
        self.login()
        with patch.object(self.server.downloads, "enqueue", return_value={"items": [], "concurrency": 1}) as enqueue:
            for invalid_headers in ({}, dict(headers, Origin="https://attacker.test"),
                                    dict(headers, **{"Sec-Fetch-Site": "cross-site"}),
                                    dict(headers, **{"X-Nass3cp-Request": "search"})):
                self.assertEqual(self.request("/api/downloads", invalid_headers, "POST", body)[0], 403)
            enqueue.assert_not_called()
            self.assertEqual(self.request("/api/downloads", headers, "POST", body)[0], 202)
            enqueue.assert_called_once_with({"paths": ["/share/movie.mp4"]})
        self.assertEqual(self.request("/api/downloads/" + identifier + "/file")[0], 404)
        self.assertEqual(self.request("/api/downloads/" + identifier + "/cancel", headers, "POST", "{}")[0], 404)
        self.assertEqual(self.request("/api/downloads", headers, "POST", "[]")[0], 400)
        self.assertEqual(json.loads(self.request("/api/downloads")[2]), {"items": [], "concurrency": 1})
        self.assertEqual(self.request("/downloads.js")[0], 200)
        self.assertEqual(self.api.mock_calls, [])

    def test_search_proxy_requires_session_and_same_origin_json_requests(self):
        identifier = "a" * 32
        body = json.dumps({"path": "/share", "pattern": "报告.*", "regex": True})
        headers = {"Content-Type": "application/json", "X-Nass3cp-Request": "search"}
        self.api.start_search.return_value = {"id": identifier, "status": "running", "results": []}
        self.assertEqual(self.request("/api/searches", headers, "POST", body)[0], 403)
        self.login()
        self.assertEqual(self.request("/api/searches", {}, "POST", body)[0], 403)
        foreign = dict(headers, Origin="https://attacker.test")
        self.assertEqual(self.request("/api/searches", foreign, "POST", body)[0], 403)
        self.api.start_search.assert_not_called()
        status, _, value = self.request("/api/searches", headers, "POST", body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(value)["id"], identifier)
        self.api.start_search.assert_called_once_with("/share", "报告.*", True, False)
        self.api.search_state.return_value = {"id": identifier, "status": "completed", "results": []}
        self.assertEqual(self.request("/api/searches/" + identifier + "?cursor=100")[0], 200)
        self.api.search_state.assert_called_once_with(identifier, 100)
        self.assertEqual(self.request("/api/searches/" + "b" * 32)[0], 404)
        self.api.cancel_search.return_value = {"id": identifier, "status": "completed", "results": []}
        self.assertEqual(self.request("/api/searches/" + identifier + "/cancel", headers, "POST", "{}")[0], 200)
        self.api.cancel_search.assert_called_once_with(identifier)

    def test_search_script_and_headers_allow_only_local_script_and_requests(self):
        self.login()
        status, headers, body = self.request("/browse.js")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("Estimated progress", body)
        self.assertIn("sessionStorage", body)

class BrowserLifecycleTests(unittest.TestCase):
    def test_startup_failure_does_not_listen_or_open_browser(self):
        api = Mock()
        api.list_directory.side_effect = AuthenticationError("rejected")
        with patch("nass3cp.browse.BrowserServer") as server, patch(
            "nass3cp.browse.webbrowser.open"
        ) as open_browser, self.assertRaises(AuthenticationError):
            run_browser(api, ".")
        server.assert_not_called()
        open_browser.assert_not_called()

    def test_ctrl_c_closes_the_listener_and_prints_an_available_local_url(self):
        api = Mock()
        api.list_directory.return_value = {"entries": [], "next_cursor": None}
        servers = []

        def interrupted(server, **kwargs):
            servers.append(server)
            raise KeyboardInterrupt

        output = io.StringIO()
        with patch.object(BrowserServer, "serve_forever", interrupted), patch(
            "nass3cp.browse.webbrowser.open"
        ) as open_browser, redirect_stdout(output), self.assertRaises(KeyboardInterrupt):
            run_browser(api, ".", port=0, open_browser=False)
        open_browser.assert_not_called()
        self.assertEqual(servers[0].socket.fileno(), -1)
        self.assertIn("http://localhost:", output.getvalue())
        self.assertIn("Ctrl+C", output.getvalue())

    def test_browser_open_failure_keeps_the_server_available(self):
        api = Mock()
        api.list_directory.return_value = {"entries": [], "next_cursor": None}
        with patch.object(BrowserServer, "serve_forever") as serve, patch(
            "nass3cp.browse.webbrowser.open", side_effect=OSError("not installed")
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            run_browser(api, ".", port=0)
        serve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
