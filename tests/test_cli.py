import argparse
import os
import unittest
from unittest.mock import patch

from nass3cp.cli import _base_url, _password


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


if __name__ == "__main__":
    unittest.main()
