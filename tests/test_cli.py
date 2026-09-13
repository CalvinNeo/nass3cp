import argparse
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
from nass3cp.errors import AuthenticationError, Nass3cpError


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

    def test_saved_password_avoids_prompt(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "nass3cp.cli.getpass.getpass"
        ) as prompt:
            self.assertEqual(_password(self._args(), "saved-password"), "saved-password")
        prompt.assert_not_called()

    def test_inflight_defaults_to_three(self):
        args = build_parser().parse_args(
            ["--host", "nas.example", "source.bin", "nas:destination.bin"]
        )
        _validate_args(args)
        self.assertEqual(args.inflight, 3)

    def test_recursive_policy_defaults_to_auto(self):
        args = build_parser().parse_args(
            ["--host", "nas.example", "-r", "source", "nas:destination"]
        )
        _validate_args(args)
        self.assertTrue(args.recursive)
        self.assertEqual(args.rpolicy, "auto")
        self.assertFalse(args.dry)

    def test_recursive_copy_maps_nas_root_to_first_allowed_root(self):
        args = build_parser().parse_args(
            ["--host", "nas.example", "-r", "source", "nas:"]
        )
        source_remote, destination_remote = _validate_args(args)
        self.assertIsNone(source_remote)
        self.assertEqual(destination_remote, ".")

    def test_dry_requires_recursive_copy(self):
        args = build_parser().parse_args(
            ["--host", "nas.example", "--dry", "source", "nas:destination"]
        )
        with self.assertRaises(Nass3cpError) as caught:
            _validate_args(args)
        self.assertIn("--recursive", str(caught.exception))

    def test_recursive_copy_rejects_overwrite(self):
        args = build_parser().parse_args(
            [
                "--host",
                "nas.example",
                "--recursive",
                "--overwrite",
                "source",
                "nas:destination",
            ]
        )
        with self.assertRaises(Nass3cpError) as caught:
            _validate_args(args)
        self.assertIn("skip existing", str(caught.exception))

    def test_resume_is_limited_to_single_file_copies(self):
        args = build_parser().parse_args(
            [
                "--host",
                "nas.example",
                "--recursive",
                "--resume",
                "source",
                "nas:destination",
            ]
        )
        with self.assertRaises(Nass3cpError) as caught:
            _validate_args(args)
        self.assertIn("single-file", str(caught.exception))

    def test_resume_flag_is_routed_to_single_file_upload(self):
        api = Mock()
        with patch("nass3cp.cli.ApiClient", return_value=api), patch(
            "nass3cp.cli.upload"
        ) as upload:
            main(
                [
                    "--host",
                    "nas.example",
                    "--token",
                    "password",
                    "--resume",
                    "source.bin",
                    "nas:destination.bin",
                ]
            )

        upload.assert_called_once_with(
            api,
            "source.bin",
            "destination.bin",
            False,
            2,
            24 * 3600,
            False,
            3,
            resume=True,
        )

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

    def test_recursive_dry_upload_is_routed_with_selected_policy(self):
        api = Mock()
        with patch("nass3cp.cli.ApiClient", return_value=api), patch(
            "nass3cp.cli.recursive_upload_directory"
        ) as recursive:
            main(
                [
                    "--host",
                    "nas.example",
                    "--token",
                    "password",
                    "-r",
                    "--rpolicy=raw",
                    "--dry",
                    "source",
                    "nas:destination",
                ]
            )

        recursive.assert_called_once_with(
            api,
            "source",
            "destination",
            "raw",
            True,
            2,
            3,
            24 * 3600,
            False,
        )

    def test_recursive_download_is_routed(self):
        api = Mock()
        with patch("nass3cp.cli.ApiClient", return_value=api), patch(
            "nass3cp.cli.recursive_download_directory"
        ) as recursive:
            main(
                [
                    "--host",
                    "nas.example",
                    "--token",
                    "password",
                    "--recursive",
                    "nas:source",
                    "destination",
                ]
            )

        recursive.assert_called_once_with(
            api,
            "source",
            "destination",
            "auto",
            False,
            2,
            3,
            24 * 3600,
            False,
        )

    def test_remember_password_validates_before_saving(self):
        store = Mock()
        store.description = "test credential store"
        api = Mock()
        output = io.StringIO()
        with patch("nass3cp.cli.CredentialStore", return_value=store), patch(
            "nass3cp.cli.ApiClient", return_value=api
        ), patch("nass3cp.cli.list_remote", return_value=[]), redirect_stderr(output):
            main(
                [
                    "--host",
                    "nas.example",
                    "--token",
                    "correct-password",
                    "--remember-password",
                    "ls",
                    "nas:",
                ]
            )

        api.check_authenticated.assert_called_once_with()
        store.save.assert_called_once_with(
            "nass3cp+https://nas.example:9443",
            "correct-password",
        )
        self.assertIn("password saved", output.getvalue())

    def test_rejected_new_password_is_never_saved(self):
        store = Mock()
        api = Mock()
        api.check_authenticated.side_effect = AuthenticationError("rejected")
        with patch("nass3cp.cli.CredentialStore", return_value=store), patch(
            "nass3cp.cli.ApiClient", return_value=api
        ), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(
                [
                    "--host",
                    "nas.example",
                    "--token",
                    "wrong-password",
                    "--remember-password",
                    "ls",
                    "nas:",
                ]
            )

        self.assertEqual(caught.exception.code, 1)
        store.save.assert_not_called()

    def test_saved_password_is_loaded_and_authenticated(self):
        store = Mock()
        store.load.return_value = "saved-password"
        api = Mock()
        with patch.dict(os.environ, {}, clear=True), patch(
            "nass3cp.cli.CredentialStore", return_value=store
        ), patch("nass3cp.cli.ApiClient", return_value=api) as api_class, patch(
            "nass3cp.cli.list_remote", return_value=[]
        ), patch("nass3cp.cli.getpass.getpass") as prompt:
            main(["--host", "nas.example", "ls", "nas:"])

        store.load.assert_called_once_with("nass3cp+https://nas.example:9443")
        api_class.assert_called_once_with(
            "https://nas.example:9443",
            "saved-password",
            ca_file=None,
            insecure=False,
        )
        api.check_authenticated.assert_called_once_with()
        prompt.assert_not_called()

    def test_no_saved_password_bypasses_the_store(self):
        store = Mock()
        api = Mock()
        with patch.dict(os.environ, {}, clear=True), patch(
            "nass3cp.cli.CredentialStore", return_value=store
        ), patch("nass3cp.cli.ApiClient", return_value=api), patch(
            "nass3cp.cli.list_remote", return_value=[]
        ), patch(
            "nass3cp.cli.getpass.getpass", return_value="prompted-password"
        ):
            main(
                [
                    "--host",
                    "nas.example",
                    "--no-saved-password",
                    "ls",
                    "nas:",
                ]
            )

        store.load.assert_not_called()
        store.save.assert_not_called()
        api.check_authenticated.assert_not_called()

    def test_rejected_saved_password_is_replaced_after_successful_retry(self):
        store = Mock()
        store.load.return_value = "old-password"
        old_api = Mock()
        old_api.check_authenticated.side_effect = AuthenticationError("rejected")
        new_api = Mock()
        with patch.dict(os.environ, {}, clear=True), patch(
            "nass3cp.cli.CredentialStore", return_value=store
        ), patch(
            "nass3cp.cli.ApiClient", side_effect=[old_api, new_api]
        ), patch(
            "nass3cp.cli.list_remote", return_value=[]
        ), patch(
            "nass3cp.cli.getpass.getpass", return_value="new-password"
        ) as prompt, redirect_stderr(io.StringIO()):
            main(["--host", "nas.example", "ls", "nas:"])

        prompt.assert_called_once_with("NAS password: ")
        new_api.check_authenticated.assert_called_once_with()
        store.save.assert_called_once_with(
            "nass3cp+https://nas.example:9443",
            "new-password",
        )

    def test_forget_password_removes_credential_without_connecting(self):
        store = Mock()
        store.delete.return_value = True
        output = io.StringIO()
        with patch("nass3cp.cli.CredentialStore", return_value=store), patch(
            "nass3cp.cli.ApiClient"
        ) as api_class, redirect_stdout(output):
            main(["--host", "nas.example", "--forget-password"])

        store.delete.assert_called_once_with("nass3cp+https://nas.example:9443")
        api_class.assert_not_called()
        self.assertIn("removed saved password", output.getvalue())

    def test_safe_name_preserves_printable_unicode(self):
        self.assertEqual(_safe_name("中文 文件"), "中文 文件")


if __name__ == "__main__":
    unittest.main()
