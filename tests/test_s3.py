import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from nass3cp.config import S3Config
from nass3cp.s3 import S3Relay


class S3SigningTests(unittest.TestCase):
    def config(self, **changes):
        values = {
            "endpoint": "https://s3.amazonaws.com",
            "bucket": "examplebucket",
            "region": "us-east-1",
            "access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "session_token": None,
            "prefix": "relay",
            "addressing_style": "virtual",
            "url_ttl_seconds": 900,
            "put_headers": {},
        }
        values.update(changes)
        return S3Config(**values)

    def test_matches_aws_published_presigning_vector(self):
        signed = S3Relay(self.config()).presign(
            "GET",
            "test.txt",
            now=datetime(2013, 5, 24, tzinfo=timezone.utc),
            expires=86400,
        )
        self.assertEqual(
            signed.url,
            "https://examplebucket.s3.amazonaws.com/test.txt?"
            "X-Amz-Algorithm=AWS4-HMAC-SHA256&"
            "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request&"
            "X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host&"
            "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404",
        )

    def test_put_header_is_returned_and_covered_by_signature(self):
        relay = S3Relay(self.config(put_headers={"x-amz-server-side-encryption": "AES256"}))
        signed = relay.presign_chunk("PUT", "a" * 32, 3, content_length=123)
        query = parse_qs(urlsplit(signed.url).query)
        self.assertEqual(
            query["X-Amz-SignedHeaders"],
            ["content-length;content-type;host;x-amz-server-side-encryption"],
        )
        self.assertEqual(
            signed.headers,
            {
                "content-length": "123",
                "content-type": "application/octet-stream",
                "x-amz-server-side-encryption": "AES256",
            },
        )

    def test_path_style_endpoint(self):
        relay = S3Relay(
            self.config(
                endpoint="https://storage.example.test/base",
                bucket="bucket-name",
                addressing_style="path",
            )
        )
        signed = relay.presign("GET", "dir/a b.txt", now=datetime(2025, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(urlsplit(signed.url).netloc, "storage.example.test")
        self.assertEqual(urlsplit(signed.url).path, "/base/bucket-name/dir/a%20b.txt")

    def test_cloudflare_r2_virtual_host_and_scope(self):
        relay = S3Relay(
            self.config(
                endpoint="https://0123456789abcdef.r2.cloudflarestorage.com",
                bucket="nass3cp-relay",
                region="auto",
                presign_unsigned_payload=True,
            )
        )
        signed = relay.presign_chunk(
            "PUT",
            "a" * 32,
            0,
            content_length=64 * 1024 * 1024,
        )
        parsed = urlsplit(signed.url)
        query = parse_qs(parsed.query)
        self.assertEqual(
            parsed.netloc,
            "nass3cp-relay.0123456789abcdef.r2.cloudflarestorage.com",
        )
        self.assertIn("%2Fauto%2Fs3%2Faws4_request", parsed.query)
        self.assertEqual(
            query["X-Amz-SignedHeaders"],
            ["content-length;content-type;host"],
        )
        self.assertEqual(query["X-Amz-Content-Sha256"], ["UNSIGNED-PAYLOAD"])
        self.assertNotIn("x-amz-server-side-encryption", signed.headers)


if __name__ == "__main__":
    unittest.main()
