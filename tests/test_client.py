import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nass3cp import client
from nass3cp.errors import ProtocolError


class FakeApi:
    def __init__(self, remote_content=b""):
        self.transfer_id = "a" * 32
        self.remote_content = remote_content
        self.objects = {}
        self.current = {}
        self.acknowledged = False
        self.aborted = False

    def create_upload(self, path, size, mtime_ns, overwrite):
        self.current = {
            "id": self.transfer_id,
            "direction": "upload",
            "status": "awaiting_upload",
            "size": size,
            "chunks": (size + 3) // 4,
            "chunk_size": 4,
            "bytes_transferred": 0,
        }
        return dict(self.current)

    def create_download(self, path):
        size = len(self.remote_content)
        self.current = {
            "id": self.transfer_id,
            "direction": "download",
            "status": "ready",
            "size": size,
            "chunks": (size + 3) // 4,
            "chunk_size": 4,
            "bytes_transferred": size,
            "sha256": hashlib.sha256(self.remote_content).hexdigest(),
            "mtime_ns": 1000000000,
        }
        for index in range(self.current["chunks"]):
            self.objects[index] = self.remote_content[index * 4 : index * 4 + 4]
        return dict(self.current)

    def urls(self, transfer_id, start, count):
        result = []
        for index in range(start, start + count):
            headers = {}
            if self.current["direction"] == "upload":
                length = min(4, self.current["size"] - index * 4)
                headers["content-length"] = str(length)
            result.append(
                {"index": index, "url": "https://s3.example.test/%d" % index, "headers": headers}
            )
        return result

    def commit(self, transfer_id, digest):
        combined = b"".join(self.objects[index] for index in sorted(self.objects))
        if hashlib.sha256(combined).hexdigest() != digest:
            raise AssertionError("client sent the wrong digest")
        self.current.update(
            {"status": "complete", "bytes_transferred": len(combined), "sha256": digest}
        )
        return dict(self.current)

    def state(self, transfer_id):
        return dict(self.current)

    def acknowledge(self, transfer_id):
        self.acknowledged = True
        return dict(self.current)

    def abort(self, transfer_id):
        self.aborted = True


def fake_data_request(api):
    def request(method, item, data=None, expected=None, attempts=4):
        index = int(item["url"].rsplit("/", 1)[1])
        if method == "PUT":
            api.objects[index] = data
            return b""
        value = api.objects[index]
        if expected is not None and len(value) != expected:
            raise ProtocolError("wrong fake chunk size")
        return value

    return request


class ClientTransferTests(unittest.TestCase):
    def test_upload_reads_hashes_and_sends_all_chunks(self):
        content = b"abcdefghij"
        api = FakeApi()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(content)
            with patch("nass3cp.client._data_request", side_effect=fake_data_request(api)):
                client.upload(api, str(source), "dest.bin", False, 2, 60, True)
        self.assertEqual(b"".join(api.objects[index] for index in sorted(api.objects)), content)
        self.assertFalse(api.aborted)

    def test_download_verifies_and_atomically_writes_file(self):
        content = b"0123456789"
        api = FakeApi(content)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "destination.bin"
            with patch("nass3cp.client._data_request", side_effect=fake_data_request(api)):
                client.download(api, "source.bin", str(destination), False, 2, 60, True)
            self.assertEqual(destination.read_bytes(), content)
            self.assertFalse(list(Path(directory).glob("*.nass3cp-part")))
        self.assertTrue(api.acknowledged)

    def test_data_plane_rejects_plain_http_before_connecting(self):
        with self.assertRaises(ProtocolError):
            client._data_request("GET", {"url": "http://example.test/a", "headers": {}})


if __name__ == "__main__":
    unittest.main()

