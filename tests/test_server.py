import hashlib
import gzip
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from nass3cp.config import S3Config, ServerConfig
from nass3cp import client, recursive
from nass3cp.client import ApiClient
from nass3cp.errors import AuthenticationError
from nass3cp.s3 import PresignedRequest
from nass3cp.server import (
    ApiError,
    Nass3cpHTTPServer,
    RequestHandler,
    ServerApp,
    TransferStore,
    check_s3,
)


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.max_objects = 0
        self.fail_deletes = False

    def put_chunk(self, transfer_id, index, data):
        self.objects[(transfer_id, index)] = data
        self.max_objects = max(self.max_objects, len(self.objects))

    def get_chunk(self, transfer_id, index):
        try:
            return io.BytesIO(self.objects[(transfer_id, index)])
        except KeyError as exc:
            raise OSError("missing fake object") from exc

    def delete_chunk(self, transfer_id, index):
        if self.fail_deletes:
            raise OSError("simulated delete failure")
        self.objects.pop((transfer_id, index), None)

    def presign_chunk(self, method, transfer_id, index, content_length=None):
        return PresignedRequest(
            url="https://s3.example.test/%s/%d" % (transfer_id, index),
            headers={},
        )

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

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition was not met before timeout")

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

    def test_pipeline_upload_verifies_and_deletes_each_chunk(self):
        content = b"abcdefghijklmnopqrstuvwxyz"
        state = self.app.create_upload(
            {
                "path": "pipeline.bin",
                "size": len(content),
                "mtime_ns": 123456789,
                "overwrite": False,
                "inflight": 3,
            }
        )
        transfer_id = state["id"]
        worker = threading.Thread(
            target=self.app._receive_upload_pipeline,
            args=(transfer_id,),
            daemon=True,
        )
        worker.start()

        for index in range(state["chunks"]):
            while index - self.app.store.get(transfer_id)["chunks_consumed"] >= 3:
                time.sleep(0.01)
            start = index * state["chunk_size"]
            data = content[start : start + state["chunk_size"]]
            self.fake.put_chunk(transfer_id, index, data)
            self.app.announce_upload_chunk(
                transfer_id,
                index,
                {"sha256": hashlib.sha256(data).hexdigest()},
            )

        self.app.commit_upload(
            transfer_id,
            {"sha256": hashlib.sha256(content).hexdigest()},
        )
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual((self.root / "pipeline.bin").read_bytes(), content)
        finished = self.app.store.get(transfer_id)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["chunks_consumed"], state["chunks"])
        self.assertLessEqual(self.fake.max_objects, 3)
        self.assertFalse(self.fake.objects)

    def test_compressed_pipeline_upload_is_decoded_and_verified(self):
        content = b"compressible recursive payload\n" * 20
        encoded = gzip.compress(content, compresslevel=6, mtime=0)
        state = self.app.create_upload(
            {
                "path": "compressed.txt",
                "size": len(encoded),
                "decoded_size": len(content),
                "compression": "gzip",
                "mtime_ns": 123456789,
                "overwrite": False,
                "inflight": 3,
            }
        )
        transfer_id = state["id"]
        worker = threading.Thread(
            target=self.app._receive_upload_pipeline,
            args=(transfer_id,),
            daemon=True,
        )
        worker.start()

        for index in range(state["chunks"]):
            while index - self.app.store.get(transfer_id)["chunks_consumed"] >= 3:
                time.sleep(0.01)
            start = index * state["chunk_size"]
            data = encoded[start : start + state["chunk_size"]]
            self.fake.put_chunk(transfer_id, index, data)
            self.app.announce_upload_chunk(
                transfer_id,
                index,
                {"sha256": hashlib.sha256(data).hexdigest()},
            )

        self.app.commit_upload(
            transfer_id,
            {
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "decoded_sha256": hashlib.sha256(content).hexdigest(),
            },
        )
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual((self.root / "compressed.txt").read_bytes(), content)
        finished = self.app.store.get(transfer_id)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["decoded_sha256"], hashlib.sha256(content).hexdigest())
        self.assertLessEqual(self.fake.max_objects, 3)
        self.assertFalse(self.fake.objects)

    def test_pipeline_upload_url_window_is_enforced(self):
        state = self.app.create_upload(
            {
                "path": "pipeline.bin",
                "size": 16,
                "overwrite": False,
                "inflight": 3,
            }
        )
        with self.assertRaises(ApiError) as caught:
            self.app.urls(state["id"], 0, 4)
        self.assertEqual(caught.exception.code, "inflight_limit")

    def test_pipeline_upload_rejects_a_bad_chunk_digest(self):
        state = self.app.create_upload(
            {
                "path": "bad-pipeline.bin",
                "size": 4,
                "overwrite": False,
                "inflight": 3,
            }
        )
        transfer_id = state["id"]
        worker = threading.Thread(
            target=self.app._receive_upload_pipeline,
            args=(transfer_id,),
            daemon=True,
        )
        with self.assertLogs("nass3cp.server", level="ERROR"):
            worker.start()
            self.fake.put_chunk(transfer_id, 0, b"data")
            self.app.announce_upload_chunk(
                transfer_id,
                0,
                {"sha256": hashlib.sha256(b"evil").hexdigest()},
            )
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(self.app.store.get(transfer_id)["status"], "error")
        self.assertFalse((self.root / "bad-pipeline.bin").exists())
        self.assertFalse(self.fake.objects)

    def test_pipeline_upload_does_not_release_slot_when_delete_fails(self):
        state = self.app.create_upload(
            {
                "path": "delete-failure.bin",
                "size": 4,
                "overwrite": False,
                "inflight": 3,
            }
        )
        transfer_id = state["id"]
        worker = threading.Thread(
            target=self.app._receive_upload_pipeline,
            args=(transfer_id,),
            daemon=True,
        )
        self.fake.put_chunk(transfer_id, 0, b"data")
        self.fake.fail_deletes = True
        with self.assertLogs("nass3cp.server", level="ERROR"), mock.patch(
            "nass3cp.server.time.sleep", return_value=None
        ):
            worker.start()
            self.app.announce_upload_chunk(
                transfer_id,
                0,
                {"sha256": hashlib.sha256(b"data").hexdigest()},
            )
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        failed = self.app.store.get(transfer_id)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["chunks_consumed"], 0)
        self.assertFalse(failed["objects_cleaned"])
        self.assertIn((transfer_id, 0), self.fake.objects)
        self.assertFalse((self.root / "delete-failure.bin").exists())

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

    def test_pipeline_download_refills_only_after_verified_ack(self):
        content = b"abcdefghijklmnopqrstuvwxyz"
        source = self.root / "pipeline-source.bin"
        source.write_bytes(content)
        state = self.app.create_download({"path": source.name, "inflight": 3})
        transfer_id = state["id"]
        worker = threading.Thread(
            target=self.app._prepare_download,
            args=(transfer_id,),
            daemon=True,
        )
        worker.start()

        for index in range(state["chunks"]):
            self.wait_until(
                lambda current=index: self.app.store.get(transfer_id)["chunks_staged"]
                > current
                or self.app.store.get(transfer_id)["status"] == "error"
            )
            current = self.app.store.get(transfer_id)
            self.assertNotEqual(current["status"], "error")
            item = self.app.urls(transfer_id, index, 1)["items"][0]
            data = self.fake.objects[(transfer_id, index)]
            digest = hashlib.sha256(data).hexdigest()
            self.assertEqual(item["sha256"], digest)
            self.app.acknowledge_download_chunk(
                transfer_id,
                index,
                {"sha256": digest},
            )
            self.assertNotIn((transfer_id, index), self.fake.objects)

        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        prepared = self.app.store.get(transfer_id)
        self.assertEqual(prepared["status"], "ready")
        self.assertEqual(prepared["sha256"], hashlib.sha256(content).hexdigest())
        self.assertLessEqual(self.fake.max_objects, 3)
        finished = self.app.acknowledge_download(transfer_id)
        self.assertEqual(finished["status"], "complete")
        self.assertTrue(finished["objects_cleaned"])

    def test_paths_outside_roots_are_rejected(self):
        outside = self.base / "outside.bin"
        outside.write_bytes(b"secret")
        with self.assertRaises(ApiError) as caught:
            self.app.create_download({"path": str(outside)})
        self.assertEqual(caught.exception.code, "path_not_allowed")

    def test_list_directory_is_sorted_and_paginated(self):
        (self.root / "zeta.bin").write_bytes(b"1234")
        (self.root / "Alpha").mkdir()
        (self.root / "middle.txt").write_bytes(b"x")

        first = self.app.list_directory({"path": ".", "cursor": 0, "limit": 2})
        second = self.app.list_directory(
            {"path": ".", "cursor": first["next_cursor"], "limit": 2}
        )
        entries = first["entries"] + second["entries"]

        self.assertEqual([entry["name"] for entry in entries], ["Alpha", "middle.txt", "zeta.bin"])
        self.assertEqual(entries[0]["type"], "directory")
        self.assertIsNone(entries[0]["size"])
        self.assertEqual(entries[1]["type"], "file")
        self.assertEqual(entries[1]["size"], 1)
        self.assertEqual(first["next_cursor"], 2)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(first["total"], 3)

    def test_list_directory_rejects_paths_outside_roots(self):
        with self.assertRaises(ApiError) as caught:
            self.app.list_directory({"path": str(self.base), "cursor": 0, "limit": 10})
        self.assertEqual(caught.exception.code, "path_not_allowed")

    def test_path_info_and_recursive_directory_creation(self):
        self.assertEqual(self.app.path_info({"path": "missing"}), {"exists": False})

        created = self.app.ensure_directory({"path": "one/two"})
        self.assertTrue(created["created"])
        self.assertTrue((self.root / "one" / "two").is_dir())
        self.assertEqual(
            self.app.path_info({"path": "one/two"})["type"],
            "directory",
        )
        self.assertFalse(self.app.ensure_directory({"path": "one/two"})["created"])

        conflict = self.root / "conflict"
        conflict.write_bytes(b"file")
        with self.assertRaises(ApiError) as caught:
            self.app.ensure_directory({"path": "conflict"})
        self.assertEqual(caught.exception.code, "path_conflict")

    def test_recursive_metadata_and_transfers_do_not_follow_symbolic_links(self):
        target = self.root / "target.bin"
        target.write_bytes(b"target")
        link = self.root / "linked.bin"
        try:
            link.symlink_to(target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest("symbolic links are unavailable: %s" % exc)

        self.assertEqual(self.app.path_info({"path": "linked.bin"})["type"], "other")
        with self.assertRaises(ApiError) as caught:
            self.app.create_download({"path": "linked.bin", "inflight": 3})
        self.assertEqual(caught.exception.code, "not_found")

    def test_transfer_store_retries_a_transient_windows_replace_failure(self):
        original_replace = os.replace
        attempts = []

        def flaky_replace(source, destination):
            attempts.append((source, destination))
            if len(attempts) == 1:
                raise PermissionError("temporarily locked")
            return original_replace(source, destination)

        with mock.patch("nass3cp.server.os.replace", side_effect=flaky_replace), mock.patch(
            "nass3cp.server.time.sleep"
        ):
            store = TransferStore(self.base / "retry-state")
            state = store.create({"status": "ready"})

        self.assertEqual(store.get(state["id"])["status"], "ready")
        self.assertEqual(len(attempts), 2)

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

    def test_resumable_upload_reuses_persisted_transfer_after_restart(self):
        request = {
            "path": "resume-upload.bin",
            "size": 12,
            "mtime_ns": 123456789,
            "overwrite": False,
            "resume": True,
        }
        created = self.app.create_upload(request)

        restarted = ServerApp(self.app.config)
        restarted.s3 = self.fake
        restarted.start_worker = lambda function, *args: None
        resumed = restarted.create_upload(
            dict(request, resume_id=created["id"])
        )

        self.assertEqual(resumed["id"], created["id"])
        self.assertTrue(resumed["resumable"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["status"], "awaiting_upload")

    def test_resumable_upload_rejects_a_changed_source_identity(self):
        request = {
            "path": "resume-upload.bin",
            "size": 12,
            "mtime_ns": 123456789,
            "overwrite": False,
            "resume": True,
        }
        created = self.app.create_upload(request)

        with self.assertRaises(ApiError) as caught:
            self.app.create_upload(
                dict(request, size=13, resume_id=created["id"])
            )

        self.assertEqual(caught.exception.code, "resume_mismatch")

    def test_resumable_download_reuses_ready_blocks_after_restart(self):
        source = self.root / "resume-download.bin"
        source.write_bytes(b"abcdefghijkl")
        request = {"path": source.name, "resume": True}
        created = self.app.create_download(request)
        self.app.store.update(
            created["id"],
            status="ready",
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            bytes_transferred=source.stat().st_size,
        )

        restarted = ServerApp(self.app.config)
        restarted.s3 = self.fake
        restarted.start_worker = lambda function, *args: None
        resumed = restarted.create_download(
            dict(request, resume_id=created["id"])
        )

        self.assertEqual(resumed["id"], created["id"])
        self.assertTrue(resumed["resumable"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["status"], "ready")

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

    def test_http_api_accepts_pipeline_chunk_ready(self):
        server = Nass3cpHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            body = json.dumps(
                {
                    "path": "api-pipeline.bin",
                    "size": 4,
                    "overwrite": False,
                    "inflight": 3,
                }
            )
            headers = {
                "Authorization": "Bearer password",
                "Content-Type": "application/json",
            }
            connection.request("POST", "/v1/transfers/upload", body=body, headers=headers)
            response = connection.getresponse()
            state = json.loads(response.read())
            self.assertEqual(response.status, 201)
            self.assertTrue(state["pipeline"])

            transfer_id = state["id"]
            self.fake.put_chunk(transfer_id, 0, b"data")
            ready_body = json.dumps({"sha256": hashlib.sha256(b"data").hexdigest()})
            connection.request(
                "POST",
                "/v1/transfers/%s/chunks/0/ready" % transfer_id,
                body=ready_body,
                headers=headers,
            )
            response = connection.getresponse()
            announced = json.loads(response.read())
            self.assertEqual(response.status, 202)
            self.assertEqual(announced["chunks_staged"], 1)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_api_client_authenticates_over_explicit_http(self):
        (self.root / "listed.bin").write_bytes(b"data")
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
            api.check_authenticated()
            listing = api.list_directory(".", 0, 1)
            self.assertEqual(listing["entries"][0]["name"], "listed.bin")
            self.assertIsNone(listing["next_cursor"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_api_client_reports_a_rejected_password_distinctly(self):
        server = Nass3cpHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = ApiClient(
                "http://127.0.0.1:%d" % server.server_port,
                "wrong-password",
                timeout=5,
            )
            with self.assertRaises(AuthenticationError):
                api.check_authenticated()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_pipeline_round_trip_keeps_cloud_window_bounded(self):
        self.app.start_worker = lambda function, *args: threading.Thread(
            target=function,
            args=args,
            daemon=True,
        ).start()
        server = Nass3cpHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def data_request(method, item, data=None, expected=None, attempts=4, progress=None):
            transfer_id, raw_index = item["url"].rsplit("/", 2)[-2:]
            index = int(raw_index)
            if method == "PUT":
                self.fake.put_chunk(transfer_id, index, data)
                if progress is not None:
                    progress(len(data))
                return b""
            response = self.fake.get_chunk(transfer_id, index)
            try:
                value = response.read()
            finally:
                response.close()
            if expected is not None and len(value) != expected:
                raise AssertionError("wrong fake chunk size")
            if progress is not None:
                progress(len(value))
            return value

        try:
            api = ApiClient(
                "http://127.0.0.1:%d" % server.server_port,
                "password",
                timeout=5,
            )
            content = bytes(range(256)) * 4
            source = self.base / "client-source.bin"
            source.write_bytes(content)
            copied_back = self.base / "client-copy.bin"
            with mock.patch("nass3cp.client._data_request", side_effect=data_request):
                client.upload(
                    api,
                    str(source),
                    "round-trip.bin",
                    False,
                    8,
                    5,
                    True,
                    3,
                )
                client.download(
                    api,
                    "round-trip.bin",
                    str(copied_back),
                    False,
                    8,
                    5,
                    True,
                    3,
                )
            self.assertEqual(copied_back.read_bytes(), content)
            self.assertLessEqual(self.fake.max_objects, 3)
            self.assertFalse(self.fake.objects)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_resumable_single_file_round_trip(self):
        self.app.start_worker = lambda function, *args: threading.Thread(
            target=function,
            args=args,
            daemon=True,
        ).start()
        server = Nass3cpHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def data_request(method, item, data=None, expected=None, attempts=4, progress=None):
            transfer_id, raw_index = item["url"].rsplit("/", 2)[-2:]
            index = int(raw_index)
            if method == "PUT":
                self.fake.put_chunk(transfer_id, index, data)
                return b""
            response = self.fake.get_chunk(transfer_id, index)
            try:
                value = response.read()
            finally:
                response.close()
            if expected is not None and len(value) != expected:
                raise AssertionError("wrong fake chunk size")
            return value

        try:
            api = ApiClient(
                "http://127.0.0.1:%d" % server.server_port,
                "password",
                timeout=5,
            )
            content = b"abcdefghijkl"
            source = self.base / "resumable-client-source.bin"
            source.write_bytes(content)
            copied_back = self.base / "resumable-client-copy.bin"
            with mock.patch("nass3cp.client._data_request", side_effect=data_request):
                client.upload(
                    api,
                    str(source),
                    "resumable-round-trip.bin",
                    False,
                    2,
                    5,
                    True,
                    resume=True,
                )
                client.download(
                    api,
                    "resumable-round-trip.bin",
                    str(copied_back),
                    False,
                    2,
                    5,
                    True,
                    resume=True,
                )

            self.assertEqual(copied_back.read_bytes(), content)
            self.assertFalse(client._resume_path(source, "upload").exists())
            self.assertFalse(client._resume_path(copied_back, "download").exists())
            self.wait_until(lambda: not self.fake.objects)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_compressed_pipeline_round_trip_is_transparent(self):
        self.app.start_worker = lambda function, *args: threading.Thread(
            target=function,
            args=args,
            daemon=True,
        ).start()
        server = Nass3cpHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def data_request(method, item, data=None, expected=None, attempts=4, progress=None):
            transfer_id, raw_index = item["url"].rsplit("/", 2)[-2:]
            index = int(raw_index)
            if method == "PUT":
                self.fake.put_chunk(transfer_id, index, data)
                if progress is not None:
                    progress(len(data))
                return b""
            response = self.fake.get_chunk(transfer_id, index)
            try:
                value = response.read()
            finally:
                response.close()
            if expected is not None and len(value) != expected:
                raise AssertionError("wrong fake chunk size")
            if progress is not None:
                progress(len(value))
            return value

        try:
            api = ApiClient(
                "http://127.0.0.1:%d" % server.server_port,
                "password",
                timeout=5,
            )
            content = b"recursive compression over the relay\n" * 20
            encoded = gzip.compress(content, compresslevel=6, mtime=0)
            encoded_source = self.base / "encoded.gz"
            encoded_source.write_bytes(encoded)
            copied_back = self.base / "decoded-copy.txt"
            with mock.patch("nass3cp.client._data_request", side_effect=data_request):
                client.upload(
                    api,
                    str(encoded_source),
                    "compressed-round-trip.txt",
                    False,
                    8,
                    5,
                    True,
                    3,
                    compression="gzip",
                    decoded_size=len(content),
                    decoded_digest=hashlib.sha256(content).hexdigest(),
                )
                self.assertEqual(
                    (self.root / "compressed-round-trip.txt").read_bytes(),
                    content,
                )
                client.download(
                    api,
                    "compressed-round-trip.txt",
                    str(copied_back),
                    False,
                    8,
                    5,
                    True,
                    3,
                    compression="gzip",
                )
            self.assertEqual(copied_back.read_bytes(), content)
            self.assertLessEqual(self.fake.max_objects, 3)
            self.assertFalse(self.fake.objects)
            self.assertFalse(list((self.base / "state" / "compressed").glob("*.gz")))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_recursive_copy_merges_skips_and_preserves_empty_directories(self):
        self.app.start_worker = lambda function, *args: threading.Thread(
            target=function,
            args=args,
            daemon=True,
        ).start()
        server = Nass3cpHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def data_request(method, item, data=None, expected=None, attempts=4, progress=None):
            transfer_id, raw_index = item["url"].rsplit("/", 2)[-2:]
            index = int(raw_index)
            if method == "PUT":
                self.fake.put_chunk(transfer_id, index, data)
                if progress is not None:
                    progress(len(data))
                return b""
            response = self.fake.get_chunk(transfer_id, index)
            try:
                value = response.read()
            finally:
                response.close()
            if expected is not None and len(value) != expected:
                raise AssertionError("wrong fake chunk size")
            if progress is not None:
                progress(len(value))
            return value

        try:
            api = ApiClient(
                "http://127.0.0.1:%d" % server.server_port,
                "password",
                timeout=5,
            )
            local_source = self.base / "recursive-source"
            (local_source / "nested").mkdir(parents=True)
            (local_source / "empty").mkdir()
            notes = b"recursive text payload\n" * 12
            binary = bytes(range(12))
            (local_source / "notes.txt").write_bytes(notes)
            (local_source / "empty.txt").write_bytes(b"")
            (local_source / "nested" / "clip.mp4").write_bytes(binary)
            (local_source / "existing.txt").write_bytes(b"source replacement")
            remote_target = self.root / "recursive-target"
            remote_target.mkdir()
            (remote_target / "existing.txt").write_bytes(b"keep on NAS")

            with mock.patch("nass3cp.client._data_request", side_effect=data_request):
                upload_plan = recursive.recursive_upload_directory(
                    api,
                    str(local_source),
                    "recursive-target",
                    "auto",
                    False,
                    4,
                    3,
                    5,
                    True,
                )

                local_target = self.base / "recursive-copy"
                local_target.mkdir()
                (local_target / "existing.txt").write_bytes(b"keep locally")
                download_plan = recursive.recursive_download_directory(
                    api,
                    "recursive-target",
                    str(local_target),
                    "auto",
                    False,
                    4,
                    3,
                    5,
                    True,
                )

            self.assertEqual(len(upload_plan.transfers), 3)
            self.assertEqual(len(upload_plan.skipped), 1)
            self.assertEqual((remote_target / "existing.txt").read_bytes(), b"keep on NAS")
            self.assertEqual((remote_target / "notes.txt").read_bytes(), notes)
            self.assertEqual((remote_target / "empty.txt").read_bytes(), b"")
            self.assertEqual(
                (remote_target / "notes.txt").stat().st_mtime_ns,
                (local_source / "notes.txt").stat().st_mtime_ns,
            )
            self.assertEqual((remote_target / "nested" / "clip.mp4").read_bytes(), binary)
            self.assertTrue((remote_target / "empty").is_dir())
            self.assertEqual(len(download_plan.transfers), 3)
            self.assertEqual(len(download_plan.skipped), 1)
            self.assertEqual((local_target / "existing.txt").read_bytes(), b"keep locally")
            self.assertEqual((local_target / "notes.txt").read_bytes(), notes)
            self.assertEqual((local_target / "empty.txt").read_bytes(), b"")
            self.assertEqual(
                (local_target / "notes.txt").stat().st_mtime_ns,
                (remote_target / "notes.txt").stat().st_mtime_ns,
            )
            self.assertEqual((local_target / "nested" / "clip.mp4").read_bytes(), binary)
            self.assertTrue((local_target / "empty").is_dir())
            self.assertLessEqual(self.fake.max_objects, 3)
            self.assertFalse(self.fake.objects)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
