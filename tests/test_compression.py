import gzip
import io
import unittest

from nass3cp.compression import DecodingWriter, gzip_compress_stream, should_compress


class CompressionTests(unittest.TestCase):
    def test_suffix_policy_targets_text_and_database_files(self):
        self.assertTrue(should_compress("REPORT.JSON"))
        self.assertTrue(should_compress("database.sqlite"))
        self.assertFalse(should_compress("movie.mp4"))
        self.assertFalse(should_compress("archive.gz"))

    def test_deterministic_gzip_round_trip_and_hashes(self):
        content = (b"highly compressible text\n" * 1000) + b"end"
        compressed = io.BytesIO()
        source_digest, source_size = gzip_compress_stream(io.BytesIO(content), compressed)
        wire = compressed.getvalue()
        output = io.BytesIO()
        writer = DecodingWriter(output, "gzip", len(content))
        for start in range(0, len(wire), 17):
            writer.write(wire[start : start + 17])
        wire_digest, decoded_digest = writer.finish()

        self.assertEqual(source_size, len(content))
        self.assertEqual(gzip.decompress(wire), content)
        self.assertEqual(output.getvalue(), content)
        self.assertEqual(decoded_digest, source_digest)
        self.assertEqual(len(wire_digest), 64)

    def test_decoder_rejects_content_larger_than_declared(self):
        wire = gzip.compress(b"too large")
        writer = DecodingWriter(io.BytesIO(), "gzip", 3)
        with self.assertRaises(ValueError):
            writer.write(wire)

    def test_decoder_rejects_trailing_gzip_data(self):
        wire = gzip.compress(b"data") + b"trailing"
        writer = DecodingWriter(io.BytesIO(), "gzip", 4)
        writer.write(wire)
        with self.assertRaises(ValueError):
            writer.finish()


if __name__ == "__main__":
    unittest.main()
