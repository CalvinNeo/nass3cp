import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit

from nass3cp.browse import BrowserServer
from nass3cp.client import ApiClient
from nass3cp.config import NatholeConfig, S3Config, ServerConfig, load_server_config
from nass3cp.direct import DirectTransfers
from nass3cp.downloads import DownloadError, DownloadManager
from nass3cp.errors import ConfigError, DownloadCancelled, Nass3cpError, ProtocolError
from nass3cp.nathole import ClientTunnel, ServerTunnels, ServiceProcess
from nass3cp.server import Nass3cpHTTPServer, RequestHandler, ServerApp, run
from nass3cp.uploads import UploadError, UploadManager


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / "third_party" / "nathole" / "nat4_tunnel.py"


def config_for(base, nathole=None):
    root = base / "share"
    root.mkdir(exist_ok=True)
    return ServerConfig("127.0.0.1", 9443, False, None, None,
                        hashlib.sha256(b"test-password").hexdigest(), [root], base / "state",
                        5 * 1024 * 1024, 3600, 20 * 1024 * 1024,
                        S3Config("https://unused.invalid", "bucket", "auto", "access", "secret",
                                 None, "relay", "path", 900, {}), nathole=nathole)


class DisabledNatholeTests(unittest.TestCase):
    def test_absent_disabled_and_omitted_enabled_do_not_expand_nathole_or_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = config_for(base)
            raw = {"tls": {"enabled": False}, "auth": {"password": "test-password"},
                   "allowed_roots": [str(config.allowed_roots[0])], "state_dir": str(config.state_dir),
                   "s3": {"endpoint": "https://unused.invalid", "bucket": "bucket", "region": "auto",
                          "access_key_id": "access", "secret_access_key": "secret"}}
            for block in (None, {}, {"enabled": False, "program": "${NASS3CP_TEST_UNSET_PROGRAM}",
                                     "keys_dir": "${NASS3CP_TEST_UNSET_KEYS}"}):
                candidate = dict(raw)
                if block is not None:
                    candidate["nathole"] = block
                filename = base / "config.json"
                filename.write_text(json.dumps(candidate), encoding="utf-8")
                loaded = load_server_config(str(filename))
                self.assertIsNone(loaded.nathole)
                with patch("subprocess.Popen", side_effect=AssertionError("unexpected child process")), patch.object(
                        Nass3cpHTTPServer, "serve_forever", side_effect=KeyboardInterrupt), patch.object(
                        ServerApp, "start_worker"), self.assertRaises(KeyboardInterrupt):
                    run(replace(loaded, port=0))
                self.assertFalse((config.state_dir / "nathole").exists())

    def test_enabled_nathole_can_omit_s3_but_bad_feature_flags_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            raw = {"tls": {"enabled": False}, "auth": {"password": "password"},
                   "allowed_roots": [str(base / "share")],
                   "nathole": {"enabled": True, "coordinator": {"host": "127.0.0.1", "port": 40000}}}
            filename = base / "config.json"
            filename.write_text(json.dumps(raw), encoding="utf-8")
            loaded = load_server_config(str(filename))
            self.assertIsNone(loaded.s3)
            self.assertEqual(loaded.nathole.server, "127.0.0.1:40000")
            for flag in (1, "true", None):
                raw["nathole"]["enabled"] = flag
                filename.write_text(json.dumps(raw), encoding="utf-8")
                with self.subTest(flag=flag), self.assertRaises(ConfigError):
                    load_server_config(str(filename))

    def test_queue_rejects_unavailable_transport_before_any_remote_work(self):
        api = Mock()
        with self.assertRaises(DownloadError):
            DownloadManager(api).enqueue({"paths": ["hello.txt"], "transport": "nathole"})
        with self.assertRaises(UploadError):
            UploadManager(api).enqueue({"path": ".", "files": [], "transport": "nathole"})
        api.assert_not_called()

    def test_untrusted_registration_is_rejected_before_creating_keys(self):
        api = ApiClient("http://nas.example:9443", "password")
        tunnel = ClientTunnel(api, {"server_id": "a" * 32, "coordinator": "127.0.0.1:40000"})
        with patch.dict(os.environ, {"NASS3CP_NATHOLE_KEYS": ""}), patch("subprocess.Popen") as spawn:
            with self.assertRaisesRegex(Nass3cpError, "verified HTTPS"):
                tunnel.endpoint(threading.Event())
            spawn.assert_not_called()


class DirectFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nass3cp-direct-test-")
        self.base = Path(self.temporary.name)
        self.config = config_for(self.base, NatholeConfig("127.0.0.1:40000", PROGRAM))
        self.app = ServerApp(self.config)
        self.app.nathole = Mock()
        self.app.nathole.info.return_value = {"server_id": "a" * 32, "coordinator": "127.0.0.1:40000"}
        self.server = Nass3cpHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient("http://127.0.0.1:%d" % self.server.server_port, "test-password", trusted_tunnel=True)
        self.direct = DirectTransfers(SimpleNamespace(endpoint=lambda _: self.api))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.app.stop()
        self.temporary.cleanup()

    def request(self, method, path, payload=None, digest=None, password="test-password"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"Authorization": "Bearer " + password}
        if payload is not None:
            headers["X-Nass3cp-SHA256"] = digest or hashlib.sha256(payload).hexdigest()
        try:
            connection.request(method, "/v1/files?" + urlencode({"path": path}), body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_whole_file_round_trip_unicode_zero_bytes_and_no_overwrite(self):
        for index, payload in enumerate((b"", bytes(range(256)) * 4096)):
            source = self.base / ("local-%d" % index)
            source.write_bytes(payload)
            name = "文件-%d.dat" % index
            self.direct.upload(str(source), name, 123456789000, lambda *args: None, threading.Event())
            self.assertEqual((self.config.allowed_roots[0] / name).read_bytes(), payload)
            target = self.base / ("download-%d" % index)
            self.direct.download(name, str(target), lambda *args: None, threading.Event())
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(self.request("PUT", name, b"replacement")[0], 409)
            self.assertEqual((self.config.allowed_roots[0] / name).read_bytes(), payload)
            self.assertFalse(list(self.config.allowed_roots[0].glob(".nass3cp-direct-*.part")))

    def test_checksum_failure_boundaries_auth_and_disabled_routes(self):
        self.assertEqual(self.request("PUT", "bad.txt", b"bad", "0" * 64)[0], 400)
        self.assertFalse((self.config.allowed_roots[0] / "bad.txt").exists())
        self.assertEqual(self.request("PUT", "../escape.txt", b"escape")[0], 403)
        self.assertEqual(self.request("GET", "hello", password="wrong")[0], 401)
        self.app.nathole = None
        self.assertEqual(self.request("PUT", "disabled.txt", b"disabled")[0], 404)
        self.assertEqual(self.request("GET", "hello")[0], 404)
        self.assertFalse(list(self.config.allowed_roots[0].glob(".nass3cp-direct-*.part")))

    def test_incomplete_upload_is_cleaned_and_never_published(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.connect()
        connection.putrequest("PUT", "/v1/files?path=incomplete.txt")
        connection.putheader("Authorization", "Bearer test-password")
        connection.putheader("Content-Length", "100")
        connection.putheader("X-Nass3cp-SHA256", "0" * 64)
        connection.endheaders()
        connection.send(b"short")
        connection.sock.shutdown(1)
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.close()
        self.assertFalse((self.config.allowed_roots[0] / "incomplete.txt").exists())
        self.assertFalse(list(self.config.allowed_roots[0].glob(".nass3cp-direct-*.part")))

    def test_cancelled_download_removes_partial_but_existing_destination_is_kept(self):
        payload = b"a" * (256 * 1024)
        (self.config.allowed_roots[0] / "file.bin").write_bytes(payload)
        target = self.base / "destination.bin"
        cancelled = threading.Event()
        def progress(label, received, total):
            if received:
                cancelled.set()
        with self.assertRaises(DownloadCancelled):
            self.direct.download("file.bin", str(target), progress, cancelled)
        self.assertFalse(target.exists())
        target.write_bytes(b"keep")
        with self.assertRaises(Nass3cpError):
            self.direct.download("file.bin", str(target), lambda *args: None, threading.Event())
        self.assertEqual(target.read_bytes(), b"keep")

    def test_direct_transfer_slot_is_shared_between_upload_and_download(self):
        entered = threading.Event()
        self.direct.lock.acquire()
        cancelled = threading.Event()
        errors = []
        def wait():
            try:
                with self.direct._connection(cancelled, DownloadCancelled, lambda *args: entered.set()):
                    self.fail("the occupied slot was entered")
            except DownloadCancelled:
                errors.append("cancelled")
        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(entered.wait(2))
        cancelled.set()
        thread.join(2)
        self.direct.lock.release()
        self.assertEqual(errors, ["cancelled"])
        self.app.direct_lock.acquire()
        try:
            self.assertEqual(self.request("PUT", "busy.txt", b"data")[0], 409)
        finally:
            self.app.direct_lock.release()

    def test_remote_plain_http_cannot_register_new_credentials(self):
        with patch("nass3cp.server.ipaddress.ip_address", return_value=SimpleNamespace(is_loopback=False)):
            with self.assertRaises(ProtocolError) as error:
                self.api.request("POST", "/v1/nathole/peers", {})
        self.assertEqual(error.exception.__cause__.code, 403)
        self.app.nathole.register.assert_not_called()


@unittest.skipUnless(PROGRAM.is_file(), "optional nathole submodule is not initialized")
class NatholeIntegrationTests(unittest.TestCase):
    def test_shutdown_during_process_creation_reaps_the_late_child(self):
        with tempfile.TemporaryDirectory(prefix="nass3cp-nathole-stop-") as temporary:
            configuration = Path(temporary) / "service.json"
            configuration.write_text('{"version": 1, "peers": []}', encoding="utf-8")
            service = ServiceProcess(PROGRAM, configuration)
            created, release = threading.Event(), threading.Event()
            children = []
            original_spawn = subprocess.Popen

            def delayed_spawn(*args, **kwargs):
                process = original_spawn(*args, **kwargs)
                children.append(process)
                created.set()
                if not release.wait(5):
                    process.kill()
                    raise RuntimeError("test did not release the child")
                return process

            with patch("nass3cp.nathole.subprocess.Popen", side_effect=delayed_spawn):
                service.thread = threading.Thread(target=service._monitor, daemon=True)
                service.thread.start()
                closing = None
                try:
                    self.assertTrue(created.wait(5))
                    closing = threading.Thread(target=service.close, daemon=True)
                    closing.start()
                    self.assertTrue(service.closed.wait(2))
                    release.set()
                    closing.join(5)
                    self.assertFalse(closing.is_alive(), "shutdown left a daemon running")
                    self.assertFalse(service.thread.is_alive())
                    self.assertIsNotNone(children[0].poll())
                finally:
                    release.set()
                    for process in children:
                        if process.poll() is None:
                            process.kill()
                            process.wait(5)
                    if closing:
                        closing.join(5)
                    service.close()

    def test_browser_enrolls_and_transfers_through_real_supervised_tunnel(self):
        sys.path.insert(0, str(PROGRAM.parent))
        try:
            import nat4_demo as n
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="nass3cp-nathole-test-") as temporary:
            base = Path(temporary)
            with n.Server(("127.0.0.1", 0), n.ServerConfig(rounds=2, round_ms=700)) as coordinator:
                config = config_for(base, NatholeConfig(n.addr_text(coordinator.address), PROGRAM))
                app = ServerApp(config)
                app.s3 = Mock()
                server = Nass3cpHTTPServer(("127.0.0.1", 0), RequestHandler, app)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                app.nathole = ServerTunnels(config.nathole, config.state_dir, n.addr_text(server.server_address))
                browser = None
                try:
                    app.nathole.start()
                    api = ApiClient("http://" + n.addr_text(server.server_address), "test-password", trusted_tunnel=True)
                    info = api.request("GET", "/v1/health")
                    self.assertEqual(info["transports"], ["s3", "nathole"])
                    browser = BrowserServer(api, ".", port=0, capabilities=info, download_concurrency=4)
                    browser_thread = threading.Thread(target=browser.serve_forever, daemon=True)
                    browser_thread.start()
                    cookie = None
                    def request(method, path, body=None, action=None):
                        headers = {"Cookie": cookie} if cookie else {}
                        if action:
                            headers["X-Nass3cp-Request"] = action
                        if isinstance(body, dict):
                            body = json.dumps(body).encode("utf-8")
                            headers["Content-Type"] = "application/json"
                        elif isinstance(body, bytes):
                            headers["Content-Type"] = "application/octet-stream"
                        connection = http.client.HTTPConnection("127.0.0.1", browser.server_port, timeout=10)
                        try:
                            connection.request(method, path, body=body, headers=headers)
                            response = connection.getresponse()
                            return response.status, dict(response.getheaders()), response.read()
                        finally:
                            connection.close()
                    def wait_item(kind, identifier, status):
                        deadline = time.monotonic() + 45
                        while time.monotonic() < deadline:
                            code, _, data = request("GET", "/api/" + kind)
                            self.assertEqual(code, 200)
                            item = next(value for value in json.loads(data)["items"] if value["id"] == identifier)
                            if item["status"] == status:
                                return item
                            self.assertNotIn(item["status"], ("failed", "cancelled"), item)
                            time.sleep(0.05)
                        self.fail("transfer did not finish: " + repr(item))
                    launch = urlsplit(browser.url)
                    status, headers, _ = request("GET", launch.path + "?" + launch.query)
                    self.assertEqual(status, 303)
                    cookie = headers["Set-Cookie"].split(";", 1)[0]
                    _, _, page = request("GET", "/")
                    self.assertIn(b'id="download-transport"', page)
                    self.assertIn(b'value="nathole"', page)
                    self.assertIn(b'value="s3"', page)
                    self.assertFalse(list(app.nathole.peer_directory.iterdir()))
                    payload = bytes(range(256)) * 1024
                    with patch("nass3cp.nathole.client_state_directory", return_value=base / "client-state"), patch.dict(
                            os.environ, {"NASS3CP_NATHOLE_KEYS": "", "NASS3CP_NATHOLE_PROGRAM": ""}):
                        code, _, data = request("POST", "/api/uploads", {"path": ".", "transport": "nathole",
                            "files": [{"name": "测试.bin", "size": len(payload), "mtime_ms": 123000}]}, "upload")
                        self.assertEqual(code, 202)
                        upload_id = json.loads(data)["enqueued_ids"][0]
                        code, _, data = request("PUT", "/api/uploads/" + upload_id + "/file", payload, "upload")
                        self.assertEqual(code, 202, data)
                        item = wait_item("uploads", upload_id, "complete")
                        self.assertEqual(item["transport"], "nathole")
                        self.assertEqual((config.allowed_roots[0] / "测试.bin").read_bytes(), payload)
                        code, _, data = request("POST", "/api/downloads", {
                            "paths": ["测试.bin"], "transport": "nathole"}, "download")
                        self.assertEqual(code, 202)
                        download_id = json.loads(data)["enqueued_ids"][0]
                        self.assertEqual(wait_item("downloads", download_id, "ready")["transport"], "nathole")
                        code, _, data = request("GET", "/api/downloads/" + download_id + "/file")
                        self.assertEqual((code, data), (200, payload))
                        self.assertEqual(len(list(app.nathole.peer_directory.iterdir())), 1)
                        app.s3.assert_not_called()
                        self.assertEqual(app.s3.mock_calls, [])
                        self.assertFalse(list(config.allowed_roots[0].glob(".nass3cp-direct-*.part")))
                finally:
                    if browser:
                        browser.shutdown()
                        browser.server_close()
                        browser_thread.join(5)
                    app.nathole.close()
                    server.shutdown()
                    server.server_close()
                    thread.join(5)
                    app.stop()


if __name__ == "__main__":
    unittest.main()
