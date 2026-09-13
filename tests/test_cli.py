import argparse
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from nass3cp.cli import (
    _base_url,
    _password,
    _safe_name,
    _validate_args,
    _validate_ls_args,
    build_parser,
    main,
)
from nass3cp.errors import Nass3cpError


class CliTests(unittest.TestCase):
    def _args(self):
        return argparse.Namespace(password_file=None, token_file=None, token=None)

    def test_no_tls_base_url_uses_http(self):
        self.assertEqual(_base_url("10.10.10.2", 9443, tls=False), "http://10.10.10.2:9443")

    def test_password_is_prompted_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "nass3cp.cli.getpass.getpass", return_value="prompted-password"
        ) as prompt:
            self.assertEqual(_password(self._args()), "prompted-password")
        prompt.assert_called_once_with("NAS password: ")

    def test_environment_password_avoids_prompt(self):
        with patch.dict(os.environ, {"NASS3CP_PASSWORD": "environment-password"}, clear=True), patch(
            "nass3cp.cli.getpass.getpass"
        ) as prompt:
            self.assertEqual(_password(self._args()), "environment-password")
        prompt.assert_not_called()

    def test_inflight_defaults_to_three(self):
        args = build_parser().parse_args(
            ["--host", "nas.example", "source.bin", "nas:destination.bin"]
        )
        _validate_args(args)
        self.assertEqual(args.inflight, 3)

    def test_inflight_must_be_in_supported_range(self):
        args = build_parser().parse_args(
            [
                "--host",
                "nas.example",
                "--inflight",
                "129",
                "source.bin",
                "nas:destination.bin",
            ]
        )
        with self.assertRaises(Nass3cpError) as caught:
            _validate_args(args)
        self.assertIn("--inflight", str(caught.exception))

    def test_ls_accepts_root_and_maps_it_to_the_first_allowed_root(self):
        args = build_parser().parse_args(["--host", "nas.example", "ls", "nas:"])
        self.assertEqual(_validate_ls_args(args), ".")

    def test_ls_requires_a_nas_prefixed_directory(self):
        args = build_parser().parse_args(["--host", "nas.example", "ls", "."])
        with self.assertRaises(Nass3cpError):
            _validate_ls_args(args)

    def test_ls_command_prints_a_safe_directory_listing(self):
        api = Mock()
        entries = [
            {"name": "folder", "type": "directory", "size": None, "mtime_ns": 0},
            {"name": "bad\x1bname", "type": "file", "size": 12, "mtime_ns": 0},
        ]
        output = io.StringIO()
        with patch("nass3cp.cli.ApiClient", return_value=api), patch(
            "nass3cp.cli.list_remote", return_value=entries
        ) as listed, redirect_stdout(output):
            main(["--host", "nas.example", "--token", "password", "ls", "nas:"])

        listed.assert_called_once_with(api, ".")
        self.assertIn("folder/", output.getvalue())
        self.assertIn("bad\\x1bname", output.getvalue())
        self.assertNotIn("bad\x1bname", output.getvalue())

    def test_safe_name_preserves_printable_unicode(self):
        self.assertEqual(_safe_name("中文 文件"), "中文 文件")


if __name__ == "__main__":
    unittest.main()
