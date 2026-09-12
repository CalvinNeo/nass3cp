import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nass3cp.config import load_server_config
from nass3cp.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def test_aliyun_example_is_parseable(self):
        filename = Path(__file__).resolve().parents[1] / "examples" / "server.aliyun.json"
        with patch.dict(
            os.environ,
            {
                "NASS3CP_TOKEN": "token",
                "ALIBABA_CLOUD_ACCESS_KEY_ID": "LTAIexample",
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "secret",
            },
            clear=False,
        ):
            config = load_server_config(str(filename))
        self.assertEqual(config.s3.endpoint, "https://s3.oss-cn-hangzhou.aliyuncs.com")
        self.assertEqual(config.chunk_size, 64 * 1024 * 1024)

    def test_loads_secrets_from_exact_environment_placeholders(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            raw = {
                "tls": {"cert_file": "cert.pem", "key_file": "key.pem"},
                "auth": {"token": "${TEST_NASS3CP_TOKEN}"},
                "allowed_roots": [str(root)],
                "s3": {
                    "endpoint": "https://s3.example.test",
                    "bucket": "bucket",
                    "region": "region",
                    "access_key_id": "${TEST_NASS3CP_ACCESS}",
                    "secret_access_key": "${TEST_NASS3CP_SECRET}",
                },
            }
            filename = base / "server.json"
            filename.write_text(json.dumps(raw), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "TEST_NASS3CP_TOKEN": "token",
                    "TEST_NASS3CP_ACCESS": "access",
                    "TEST_NASS3CP_SECRET": "secret",
                },
                clear=False,
            ):
                config = load_server_config(str(filename))
            self.assertEqual(config.s3.access_key_id, "access")
            self.assertEqual(config.state_dir, (base / "state").resolve())
            self.assertNotEqual(config.auth_token_sha256, "token")

    def test_rejects_plain_http_s3_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            raw = {
                "tls": {"cert_file": "cert.pem", "key_file": "key.pem"},
                "auth": {"token": "token"},
                "allowed_roots": [str(base)],
                "s3": {
                    "endpoint": "http://s3.example.test",
                    "bucket": "bucket",
                    "region": "region",
                    "access_key_id": "access",
                    "secret_access_key": "secret",
                },
            }
            filename = base / "server.json"
            filename.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_server_config(str(filename))


if __name__ == "__main__":
    unittest.main()
