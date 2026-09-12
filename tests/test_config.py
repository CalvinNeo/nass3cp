import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nass3cp.config import load_environment_file, load_server_config
from nass3cp.errors import ConfigError
from nass3cp.server import validate_server_config


class ConfigTests(unittest.TestCase):
    def test_aliyun_example_is_parseable(self):
        filename = Path(__file__).resolve().parents[1] / "examples" / "server.aliyun.json"
        with patch.dict(
            os.environ,
            {
                "NASS3CP_PASSWORD": "password",
                "ALIBABA_CLOUD_ACCESS_KEY_ID": "LTAIexample",
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "secret",
            },
            clear=False,
        ):
            config = load_server_config(str(filename))
        self.assertEqual(config.s3.endpoint, "https://s3.oss-cn-hangzhou.aliyuncs.com")
        self.assertEqual(config.chunk_size, 64 * 1024 * 1024)

    def test_cloudflare_r2_example_is_parseable(self):
        filename = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "server.cloudflare-r2.json"
        )
        with patch.dict(
            os.environ,
            {
                "NASS3CP_PASSWORD": "password",
                "CLOUDFLARE_R2_ACCESS_KEY_ID": "exampleaccesskey",
                "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "secret",
            },
            clear=False,
        ):
            config = load_server_config(str(filename))
        self.assertEqual(config.s3.region, "auto")
        self.assertEqual(config.s3.addressing_style, "virtual")
        self.assertTrue(config.s3.presign_unsigned_payload)
        self.assertEqual(config.s3.put_headers, {})

    def test_project_local_cloudflare_config_is_parseable(self):
        root = Path(__file__).resolve().parents[1]
        filename = root / "config" / "server.json"
        with patch.dict(
            os.environ,
            {
                "NASS3CP_PASSWORD": "password",
                "CLOUDFLARE_R2_ACCESS_KEY_ID": "exampleaccesskey",
                "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "secret",
            },
            clear=False,
        ):
            config = load_server_config(str(filename))
        self.assertEqual(config.state_dir, (root / "state").resolve())
        self.assertEqual(config.cert_file, (root / "config" / "server.crt").resolve())

    def test_environment_file_is_non_executable_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "test.env"
            filename.write_text(
                "# comment\n"
                "export TEST_NASS3CP_FROM_FILE='literal $HOME'\n"
                'TEST_NASS3CP_QUOTED="line\\nvalue"\n'
                "TEST_NASS3CP_PRECEDENCE=file\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"TEST_NASS3CP_PRECEDENCE": "environment"},
                clear=False,
            ):
                load_environment_file(str(filename))
                self.assertEqual(os.environ["TEST_NASS3CP_FROM_FILE"], "literal $HOME")
                self.assertEqual(os.environ["TEST_NASS3CP_QUOTED"], "line\nvalue")
                self.assertEqual(os.environ["TEST_NASS3CP_PRECEDENCE"], "environment")

    def test_loads_secrets_from_exact_environment_placeholders(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            raw = {
                "tls": {"cert_file": "cert.pem", "key_file": "key.pem"},
                "auth": {"password": "${TEST_NASS3CP_PASSWORD}"},
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
                    "TEST_NASS3CP_PASSWORD": "password",
                    "TEST_NASS3CP_ACCESS": "access",
                    "TEST_NASS3CP_SECRET": "secret",
                },
                clear=False,
            ):
                config = load_server_config(str(filename))
            self.assertEqual(config.s3.access_key_id, "access")
            self.assertEqual(config.state_dir, (base / "state").resolve())
            self.assertNotEqual(config.auth_password_sha256, "password")

    def test_project_local_overlay_config_is_parseable(self):
        root = Path(__file__).resolve().parents[1]
        filename = root / "config" / "server.overlay.json"
        with patch.dict(
            os.environ,
            {
                "NASS3CP_PASSWORD": "password",
                "CLOUDFLARE_R2_ACCESS_KEY_ID": "exampleaccesskey",
                "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "secret",
            },
            clear=False,
        ):
            config = load_server_config(str(filename))
        self.assertFalse(config.tls_enabled)
        self.assertEqual(config.listen, "127.0.0.1")
        self.assertIsNone(config.cert_file)

    def test_tls_disabled_needs_no_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            raw = {
                "listen": "127.0.0.1",
                "tls": {"enabled": False},
                "auth": {"password": "correct horse battery staple"},
                "allowed_roots": [str(base)],
                "s3": {
                    "endpoint": "https://s3.example.test",
                    "bucket": "bucket",
                    "region": "region",
                    "access_key_id": "access",
                    "secret_access_key": "secret",
                },
            }
            filename = base / "server.json"
            filename.write_text(json.dumps(raw), encoding="utf-8")
            config = load_server_config(str(filename))
            self.assertFalse(config.tls_enabled)
            self.assertIsNone(config.cert_file)
            self.assertIsNone(config.key_file)
            self.assertIsNone(validate_server_config(config))

    def test_tls_disabled_rejects_wildcard_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            raw = {
                "listen": "0.0.0.0",
                "tls": {"enabled": False},
                "auth": {"password": "correct horse battery staple"},
                "allowed_roots": [str(base)],
                "s3": {
                    "endpoint": "https://s3.example.test",
                    "bucket": "bucket",
                    "region": "region",
                    "access_key_id": "access",
                    "secret_access_key": "secret",
                },
            }
            filename = base / "server.json"
            filename.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "overlay IP or loopback"):
                load_server_config(str(filename))

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

    def test_legacy_token_name_remains_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            raw = {
                "tls": {"cert_file": "cert.pem", "key_file": "key.pem"},
                "auth": {"token": "legacy-token"},
                "allowed_roots": [str(base)],
                "s3": {
                    "endpoint": "https://s3.example.test",
                    "bucket": "bucket",
                    "region": "region",
                    "access_key_id": "access",
                    "secret_access_key": "secret",
                },
            }
            filename = base / "server.json"
            filename.write_text(json.dumps(raw), encoding="utf-8")
            config = load_server_config(str(filename))
            self.assertEqual(
                config.auth_password_sha256,
                hashlib.sha256(b"legacy-token").hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
