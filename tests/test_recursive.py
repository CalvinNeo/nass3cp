import gzip
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nass3cp.errors import Nass3cpError
from nass3cp.recursive import (
    _local_child,
    recursive_download_directory,
    recursive_upload_directory,
)


def _entry(name, kind, size=None, mtime_ns=1):
    return {"name": name, "type": kind, "size": size, "mtime_ns": mtime_ns}


class TreeApi:
    def __init__(self, infos=None, listings=None):
        self.infos = infos or {}
        self.listings = listings or {}
        self.created = []

    def path_info(self, path):
        return dict(self.infos.get(path, {"exists": False}))

    def list_directory(self, path, cursor=0, limit=500):
        entries = list(self.listings.get(path, []))
        page = entries[cursor : cursor + limit]
        next_cursor = cursor + len(page)
        return {
            "entries": page,
            "next_cursor": next_cursor if next_cursor < len(entries) else None,
        }

    def ensure_directory(self, path):
        self.created.append(path)
        return True


class RecursiveCopyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_windows_unsafe_remote_names_are_rejected_before_joining(self):
        root = self.base / "destination"
        with mock.patch("nass3cp.recursive.os.name", "nt"):
            with self.assertRaises(Nass3cpError):
                _local_child(root, "nested/C:escape.txt")
            with self.assertRaises(Nass3cpError):
                _local_child(root, "CON.txt")

    def test_upload_dry_run_reports_original_and_skipped_bytes_without_writes(self):
        source = self.base / "source"
        source.mkdir()
        (source / "already.txt").write_bytes(b"12345")
        (source / "new.csv").write_bytes(b"1234567")
        nested = source / "nested"
        nested.mkdir()
        (nested / "movie.mp4").write_bytes(b"abc")
        api = TreeApi(
            infos={"backup": {"exists": True, "type": "directory"}},
            listings={
                "backup": [_entry("already.txt", "file", 99)],
            },
        )
        output = io.StringIO()

        with mock.patch("nass3cp.recursive.upload") as upload:
            plan = recursive_upload_directory(
                api,
                str(source),
                "backup",
                "auto",
                True,
                2,
                3,
                60,
                True,
                output,
            )

        upload.assert_not_called()
        self.assertEqual(api.created, [])
        self.assertEqual(plan.original_size, 15)
        self.assertEqual(plan.skipped_size, 5)
        self.assertEqual(plan.transfer_size, 10)
        self.assertEqual(len(plan.compression_candidates), 1)
        rendered = output.getvalue()
        self.assertIn("source files: 3", rendered)
        self.assertIn("original size: 15 bytes", rendered)
        self.assertIn("existing files skipped: 1, 5 bytes", rendered)
        self.assertIn("dry run: no directories or files were changed", rendered)

    def test_auto_upload_compresses_suitable_files_first_and_preserves_raw_fallback(self):
        source = self.base / "source"
        source.mkdir()
        text = b"highly compressible text\n" * 200
        binary = bytes(range(64))
        text_path = source / "z-notes.txt"
        binary_path = source / "a-photo.jpg"
        text_path.write_bytes(text)
        binary_path.write_bytes(binary)
        api = TreeApi()
        captured = []

        def capture(_api, local_source, remote_destination, *args, **kwargs):
            payload_path = Path(local_source)
            payload = payload_path.read_bytes()
            captured.append(
                {
                    "path": payload_path,
                    "remote": remote_destination,
                    "payload": payload,
                    "args": args,
                    "kwargs": kwargs,
                }
            )

        with mock.patch("nass3cp.recursive.upload", side_effect=capture):
            plan = recursive_upload_directory(
                api,
                str(source),
                "backup",
                "auto",
                False,
                2,
                3,
                60,
                True,
            )

        self.assertEqual(api.created, ["backup"])
        self.assertEqual([item["remote"] for item in captured], ["backup/z-notes.txt", "backup/a-photo.jpg"])
        compressed, raw = captured
        self.assertEqual(gzip.decompress(compressed["payload"]), text)
        self.assertEqual(compressed["kwargs"]["compression"], "gzip")
        self.assertEqual(compressed["kwargs"]["decoded_size"], len(text))
        self.assertEqual(len(compressed["kwargs"]["decoded_digest"]), 64)
        self.assertFalse(compressed["path"].exists())
        self.assertEqual(raw["payload"], binary)
        self.assertIsNone(raw["kwargs"]["compression"])
        self.assertEqual(len(plan.transfers), 2)

    def test_raw_upload_does_not_compress_text(self):
        source = self.base / "source"
        source.mkdir()
        text_path = source / "notes.txt"
        text_path.write_bytes(b"repeat " * 100)
        api = TreeApi()

        with mock.patch("nass3cp.recursive.upload") as upload:
            recursive_upload_directory(
                api,
                str(source),
                "backup",
                "raw",
                False,
                2,
                3,
                60,
                True,
            )

        self.assertEqual(upload.call_count, 1)
        self.assertEqual(upload.call_args.args[1], str(text_path))
        self.assertIsNone(upload.call_args.kwargs["compression"])

    def test_auto_upload_falls_back_to_raw_when_gzip_is_not_smaller(self):
        source = self.base / "source"
        source.mkdir()
        text_path = source / "tiny.txt"
        text_path.write_bytes(b"x")
        api = TreeApi()

        with mock.patch("nass3cp.recursive.upload") as upload:
            recursive_upload_directory(
                api,
                str(source),
                "backup",
                "auto",
                False,
                2,
                3,
                60,
                True,
            )

        self.assertEqual(upload.call_args.args[1], str(text_path))
        self.assertIsNone(upload.call_args.kwargs["compression"])

    def test_download_merges_tree_skips_existing_file_and_requests_auto_compression(self):
        destination = self.base / "destination"
        destination.mkdir()
        (destination / "existing.txt").write_bytes(b"keep")
        api = TreeApi(
            infos={"source": {"exists": True, "type": "directory"}},
            listings={
                "source": [
                    _entry("docs", "directory"),
                    _entry("existing.txt", "file", 4),
                ],
                "source/docs": [
                    _entry("image.png", "file", 3),
                    _entry("notes.txt", "file", 80),
                ],
            },
        )

        with mock.patch("nass3cp.recursive.download") as download:
            plan = recursive_download_directory(
                api,
                "source",
                str(destination),
                "auto",
                False,
                2,
                3,
                60,
                True,
            )

        self.assertTrue((destination / "docs").is_dir())
        self.assertEqual((destination / "existing.txt").read_bytes(), b"keep")
        self.assertEqual(len(plan.skipped), 1)
        self.assertEqual(
            [call.args[1] for call in download.call_args_list],
            ["source/docs/notes.txt", "source/docs/image.png"],
        )
        self.assertEqual(download.call_args_list[0].kwargs["compression"], "gzip")
        self.assertIsNone(download.call_args_list[1].kwargs["compression"])

    def test_raw_download_does_not_request_compression(self):
        destination = self.base / "destination"
        api = TreeApi(
            infos={"source": {"exists": True, "type": "directory"}},
            listings={"source": [_entry("notes.txt", "file", 80)]},
        )

        with mock.patch("nass3cp.recursive.download") as download:
            recursive_download_directory(
                api,
                "source",
                str(destination),
                "raw",
                False,
                2,
                3,
                60,
                True,
            )

        self.assertEqual(download.call_count, 1)
        self.assertIsNone(download.call_args.kwargs["compression"])

    def test_download_dry_run_does_not_create_destination(self):
        destination = self.base / "new-destination"
        api = TreeApi(
            infos={"source": {"exists": True, "type": "directory"}},
            listings={"source": [_entry("notes.txt", "file", 12)]},
        )
        output = io.StringIO()

        with mock.patch("nass3cp.recursive.download") as download:
            recursive_download_directory(
                api,
                "source",
                str(destination),
                "auto",
                True,
                2,
                3,
                60,
                True,
                output,
            )

        download.assert_not_called()
        self.assertFalse(destination.exists())
        self.assertIn("original size: 12 bytes", output.getvalue())

    def test_download_dry_run_reports_a_non_directory_destination_root(self):
        destination = self.base / "destination"
        destination.write_bytes(b"not a directory")
        api = TreeApi(
            infos={"source": {"exists": True, "type": "directory"}},
            listings={
                "source": [_entry("nested", "directory")],
                "source/nested": [_entry("notes.txt", "file", 4)],
            },
        )
        output = io.StringIO()

        plan = recursive_download_directory(
            api,
            "source",
            str(destination),
            "auto",
            True,
            2,
            3,
            60,
            True,
            output,
        )

        self.assertTrue(plan.conflicts)
        self.assertIn("destination root exists as a file", output.getvalue())

    def test_structural_conflict_is_reported_before_remote_mutation(self):
        source = self.base / "source"
        (source / "folder").mkdir(parents=True)
        (source / "folder" / "item.txt").write_bytes(b"data")
        api = TreeApi(
            infos={"backup": {"exists": True, "type": "directory"}},
            listings={"backup": [_entry("folder", "file", 1)]},
        )

        with mock.patch("nass3cp.recursive.upload") as upload, self.assertRaises(
            Nass3cpError
        ) as caught:
            recursive_upload_directory(
                api,
                str(source),
                "backup",
                "auto",
                False,
                2,
                3,
                60,
                True,
                io.StringIO(),
            )

        upload.assert_not_called()
        self.assertEqual(api.created, [])
        self.assertIn("path conflict", str(caught.exception))

    def test_upload_rechecks_and_skips_a_file_that_appears_after_planning(self):
        source = self.base / "source"
        source.mkdir()
        (source / "new.txt").write_bytes(b"source")
        api = TreeApi(
            infos={
                "backup": {"exists": True, "type": "directory"},
                "backup/new.txt": {"exists": True, "type": "file"},
            },
            listings={"backup": []},
        )

        with mock.patch("nass3cp.recursive.upload") as upload:
            recursive_upload_directory(
                api,
                str(source),
                "backup",
                "auto",
                False,
                2,
                3,
                60,
                True,
            )

        upload.assert_not_called()

    def test_raw_upload_rejects_a_source_changed_after_planning(self):
        source = self.base / "source"
        source.mkdir()
        source_file = source / "data.bin"
        source_file.write_bytes(b"original")

        class MutatingApi(TreeApi):
            def path_info(self, path):
                if path == "backup/data.bin":
                    source_file.write_bytes(b"changed and longer")
                return super().path_info(path)

        api = MutatingApi(
            infos={"backup": {"exists": True, "type": "directory"}},
            listings={"backup": []},
        )

        with self.assertRaises(Nass3cpError) as caught:
            recursive_upload_directory(
                api,
                str(source),
                "backup",
                "raw",
                False,
                2,
                3,
                60,
                True,
            )

        self.assertIn("changed after recursive planning", str(caught.exception))

    def test_invalid_policy_is_rejected_before_scanning(self):
        api = mock.Mock()
        with self.assertRaises(ValueError):
            recursive_upload_directory(
                api,
                "missing",
                "backup",
                "zip",
                True,
                2,
                3,
                60,
                True,
            )
        api.path_info.assert_not_called()

    def test_local_symbolic_links_are_not_followed(self):
        source = self.base / "source"
        source.mkdir()
        outside = self.base / "outside.txt"
        outside.write_bytes(b"must not be copied")
        link = source / "linked.txt"
        try:
            link.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            self.skipTest("symbolic links are unavailable: %s" % exc)

        plan = recursive_upload_directory(
            TreeApi(),
            str(source),
            "backup",
            "auto",
            True,
            2,
            3,
            60,
            True,
            io.StringIO(),
        )

        self.assertEqual(plan.source.files, [])
        self.assertEqual(plan.source.unsupported, ["linked.txt"])
        self.assertEqual(plan.original_size, 0)


if __name__ == "__main__":
    unittest.main()
