import hashlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from nass3cp.config import S3Config, ServerConfig
from nass3cp.client import ApiClient
from nass3cp.server import (
    ApiError,
    Nass3cpHTTPServer,
    RequestHandler,
    ServerApp,
    check_s3,
)


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_chunk(self, transfer_id, index, data):
        self.objects[(transfer_id, index)] = data

    def get_chunk(self, transfer_id, index):
        try:
            return io.BytesIO(self.objects[(transfer_id, index)])
        except KeyError as exc:
            raise OSError("missing fake object") from exc

    def cleanup(self, transfer_id, chunks):
        for index in range(chunks):
            self.objects.pop((transfer_id, index), None)
        return []


class ServerTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "root"
        self.root.mkdir()
        s3 = S3Config(
            endpoint="https://s3.example.test",
            bucket="bucket",
            region="region",
            access_key_id="access",
            secret_access_key="secret",
            session_token=None,
            prefix="relay",
            addressing_style="virtual",
            url_ttl_seconds=900,
            put_headers={},
        )
        config = ServerConfig(
            listen="127.0.0.1",
            port=9443,
            tls_enabled=False,
            cert_file=self.base / "cert.pem",
            key_file=self.base / "key.pem",
            auth_password_sha256=hashlib.sha256(b"password").hexdigest(),
            allowed_roots=[self.root.resolve()],
            state_dir=self.base / "state",
            chunk_size=4,
            transfer_ttl_seconds=3600,
            max_file_size=1024,
            s3=s3,
        )
        self.app = ServerApp(config)
        self.fake = FakeS3()
        self.app.s3 = self.fake
        self.app.start_worker = lambda function, *args: None

    def tearDown(self):
        self.temporary.cleanup()

    def test_upload_is_verified_and_atomically_installed(self):
        content = b"abcdefghij"
        state = self.app.create_upload(
            {"path": "dest.bin", "size": len(content), "mtime_ns": 123456789, "overwrite": False}
        )
        transfer_id = state["id"]
        for index in range(state["chunks"]):
            start = index * state["chunk_size"]
            self.fake.objects[(transfer_id, index)] = content[start : start + state["chunk_size"]]
        self.app.store.transition(
            transfer_id,
            ("awaiting_upload",),
            "receiving",
            sha256=hashlib.sha256(content).hexdigest(),
        )

        self.app._receive_upload(transfer_id)

        self.assertEqual((self.root / "dest.bin").read_bytes(), content)
        self.assertEqual(self.app.store.get(transfer_id)["status"], "complete")
        self.assertFalse(self.fake.objects)
        self.assertFalse(list(self.root.glob(".nass3cp-*.part")))

    def test_bad_upload_digest_does_not_replace_destination(self):
        destination = self.root / "dest.bin"
        destination.write_bytes(b"original")
        state = self.app.create_upload(
            {"path": "dest.bin", "size": 4, "overwrite": True}
        )
        transfer_id = state["id"]
        self.fake.objects[(transfer_id, 0)] = b"data"
        self.app.store.transition(
            transfer_id, ("awaiting_upload",), "receiving", sha256="0" * 64
        )

        with self.assertLogs("nass3cp.server", level="ERROR"):
            self.app._receive_upload(transfer_id)

        self.assertEqual(destination.read_bytes(), b"original")
        self.assertEqual(self.app.store.get(transfer_id)["status"], "error")

    def test_download_is_split_and_hashed(self):
        content = b"0123456789"
        source = self.root / "source.bin"
        source.write_bytes(content)
        state = self.app.create_download({"path": "source.bin"})

        self.app._prepare_download(state["id"])

        finished = self.app.store.get(state["id"])
        self.assertEqual(finished["status"], "ready")
        self.assertEqual(finished["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(
            b"".join(self.fake.objects[(state["id"], index)] for index in range(3)),
            content,
        )

    def test_paths_outside_roots_are_rejected(self):
        outside = self.base / "outside.bin"
        outside.write_bytes(b"secret")
        with self.assertRaises(ApiError) as caught:
            self.app.create_download({"path": str(outside)})
        self.assertEqual(caught.exception.code, "path_not_allowed")

    def test_restart_marks_active_transfer_failed_and_removes_partial_file(self):
        state = self.app.create_upload({"path": "dest.bin", "size": 4, "overwrite": False})
        self.app.store.transition(state["id"], ("awaiting_upload",), "receiving", sha256="0" * 64)
        partial = self.app._upload_temporary_path(self.app.store.get(state["id"]))
        partial.write_bytes(b"part")

        restarted = ServerApp(self.app.config)
        restarted.s3 = self.fake
        self.assertEqual(restarted.store.get(state["id"])["status"], "error")
        restarted.cleanup_interrupted()

        self.assertFalse(partial.exists())
        self.assertTrue(restarted.store.get(state["id"])["objects_cleaned"])

    def test_authentication_uses_password_digest(self):
        self.assertTrue(self.app.authenticated("Bearer password"))
        self.assertFalse(self.app.authenticated("Bearer wrong"))
        self.assertFalse(self.app.authenticated(None))

    def test_s3_check_round_trips_and_removes_test_object(self):
        with mock.patch("nass3cp.server.S3Relay", return_value=self.fake):
            check_s3(self.app.config)
        self.assertFalse(self.fake.objects)

    def test_http_api_requires_auth_and_creates_upload(self):
        server = Nass3cpHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/v1/health")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 401)

            body = json.dumps({"path": "api.bin", "size": 10, "overwrite": False})
            connection.request(
                "POST",
                "/v1/transfers/upload",
                body=body,
                headers={
                    "Authorization": "Bearer password",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            value = json.loads(response.read())
            self.assertEqual(response.status, 201)
            self.assertEqual(value["direction"], "upload")
            self.assertEqual(value["chunks"], 3)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_api_client_authenticates_over_explicit_http(self):
        server = Nass3cpHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = ApiClient(
                "http://127.0.0.1:%d" % server.server_port,
                "password",
                timeout=5,
            )
            health = api.request("GET", "/v1/health")
            self.assertEqual(health["status"], "ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
