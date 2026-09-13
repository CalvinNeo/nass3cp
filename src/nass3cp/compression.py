import gzip
import hashlib
import zlib
from pathlib import PurePath
from typing import Any, Callable, Optional, Tuple


COMPRESSIBLE_SUFFIXES = frozenset(
    {
        ".ass",
        ".bash",
        ".bat",
        ".bib",
        ".c",
        ".cc",
        ".cfg",
        ".cmd",
        ".conf",
        ".cpp",
        ".css",
        ".csv",
        ".db",
        ".dump",
        ".eml",
        ".geojson",
        ".go",
        ".h",
        ".hpp",
        ".htm",
        ".html",
        ".ics",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsonl",
        ".jsx",
        ".kt",
        ".kts",
        ".less",
        ".log",
        ".md",
        ".ndjson",
        ".php",
        ".ps1",
        ".py",
        ".pyw",
        ".rb",
        ".rs",
        ".rst",
        ".scss",
        ".sh",
        ".sql",
        ".sqlite",
        ".sqlite3",
        ".srt",
        ".svg",
        ".tex",
        ".text",
        ".toml",
        ".ts",
        ".tsv",
        ".tsx",
        ".txt",
        ".vcf",
        ".vtt",
        ".xml",
        ".yaml",
        ".yml",
        ".zsh",
    }
)
_COPY_BLOCK_SIZE = 1024 * 1024
_DECODE_BLOCK_SIZE = 1024 * 1024


def should_compress(filename: str) -> bool:
    return PurePath(filename).suffix.lower() in COMPRESSIBLE_SUFFIXES


def gzip_compress_stream(
    source: Any,
    destination: Any,
    progress: Optional[Callable[[int], None]] = None,
) -> Tuple[str, int]:
    """Write one deterministic gzip member and return source SHA-256 and byte count."""
    digest = hashlib.sha256()
    total = 0
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=destination,
        compresslevel=6,
        mtime=0,
    ) as compressed:
        while True:
            data = source.read(_COPY_BLOCK_SIZE)
            if not data:
                break
            digest.update(data)
            compressed.write(data)
            total += len(data)
            if progress is not None:
                progress(total)
    return digest.hexdigest(), total


class DecodingWriter:
    """Hash encoded bytes while safely writing raw or gzip-decoded content."""

    def __init__(self, destination: Any, encoding: Optional[str], expected_size: int):
        if encoding not in (None, "gzip"):
            raise ValueError("unsupported content encoding")
        if expected_size < 0:
            raise ValueError("expected decoded size must not be negative")
        self.destination = destination
        self.encoding = encoding
        self.expected_size = expected_size
        self.encoded_digest = hashlib.sha256()
        self.decoded_digest = hashlib.sha256()
        self.encoded_size = 0
        self.decoded_size = 0
        self._decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None

    def _write_decoded(self, data: bytes) -> None:
        if not data:
            return
        if self.decoded_size + len(data) > self.expected_size:
            raise ValueError("decoded content exceeds its declared size")
        self.destination.write(data)
        self.decoded_digest.update(data)
        self.decoded_size += len(data)

    def write(self, data: bytes) -> None:
        self.encoded_digest.update(data)
        self.encoded_size += len(data)
        if self._decoder is None:
            self._write_decoded(data)
            return

        pending = data
        while pending:
            previous_length = len(pending)
            remaining = self.expected_size - self.decoded_size
            output = self._decoder.decompress(
                pending,
                min(_DECODE_BLOCK_SIZE, remaining + 1),
            )
            pending = self._decoder.unconsumed_tail
            self._write_decoded(output)
            if not pending:
                break
            if not output and len(pending) >= previous_length:
                raise ValueError("gzip decoder made no progress")

    def finish(self) -> Tuple[str, str]:
        if self._decoder is not None:
            if not self._decoder.eof:
                raise ValueError("gzip content is truncated")
            if self._decoder.unused_data or self._decoder.unconsumed_tail:
                raise ValueError("gzip content has trailing data")
            self._write_decoded(self._decoder.flush())
        if self.decoded_size != self.expected_size:
            raise ValueError(
                "decoded content has size %d, expected %d"
                % (self.decoded_size, self.expected_size)
            )
        return self.encoded_digest.hexdigest(), self.decoded_digest.hexdigest()
