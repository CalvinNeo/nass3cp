import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

from .errors import ConfigError


_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_PATTERN.match(value)
        if not match:
            return value
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError("environment variable %s is not set" % name)
        return os.environ[name]
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _require(mapping: Mapping[str, Any], key: str, expected_type: Any) -> Any:
    if key not in mapping:
        raise ConfigError("missing configuration key: %s" % key)
    value = mapping[key]
    if not isinstance(value, expected_type):
        raise ConfigError("configuration key %s has the wrong type" % key)
    return value


def _positive_int(mapping: Mapping[str, Any], key: str, default: int) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError("configuration key %s must be a positive integer" % key)
    return value


def _path_from_config(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str
    session_token: Optional[str]
    prefix: str
    addressing_style: str
    url_ttl_seconds: int
    put_headers: Dict[str, str]


@dataclass(frozen=True)
class ServerConfig:
    listen: str
    port: int
    cert_file: Path
    key_file: Path
    auth_token_sha256: str
    allowed_roots: List[Path]
    state_dir: Path
    chunk_size: int
    transfer_ttl_seconds: int
    max_file_size: int
    s3: S3Config


def _load_s3(raw: Mapping[str, Any]) -> S3Config:
    endpoint = _require(raw, "endpoint", str).rstrip("/")
    parsed = urlsplit(endpoint)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ConfigError("s3.endpoint has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("s3.endpoint must be an HTTPS URL without query or fragment")

    style = raw.get("addressing_style", "virtual")
    if style not in ("virtual", "path"):
        raise ConfigError("s3.addressing_style must be 'virtual' or 'path'")

    prefix_value = raw.get("prefix", "nass3cp")
    if not isinstance(prefix_value, str):
        raise ConfigError("s3.prefix must be a string")
    prefix = prefix_value.strip("/")
    if not prefix or "\x00" in prefix or len(prefix.encode("utf-8")) > 900:
        raise ConfigError("s3.prefix must not be empty")

    headers_raw = raw.get("put_headers", {})
    if not isinstance(headers_raw, dict):
        raise ConfigError("s3.put_headers must be an object")
    headers: Dict[str, str] = {}
    for name, value in headers_raw.items():
        lowered = str(name).strip().lower()
        if not re.fullmatch(r"[a-z0-9-]+", lowered):
            raise ConfigError("invalid header name in s3.put_headers")
        if not lowered.startswith(("x-amz-", "x-oss-")):
            raise ConfigError("s3.put_headers may contain only x-amz-* or x-oss-* headers")
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise ConfigError("invalid value for s3.put_headers.%s" % name)
        headers[lowered] = value.strip()

    ttl = _positive_int(raw, "url_ttl_seconds", 900)
    if ttl > 3600:
        raise ConfigError("s3.url_ttl_seconds must not exceed 3600")

    bucket = _require(raw, "bucket", str).strip()
    region = _require(raw, "region", str).strip()
    access_key_id = _require(raw, "access_key_id", str).strip()
    secret_access_key = _require(raw, "secret_access_key", str)
    session_token = raw.get("session_token")
    if not bucket or not region or not access_key_id or not secret_access_key:
        raise ConfigError("S3 bucket, region, and credentials must not be empty")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,253}[A-Za-z0-9]", bucket):
        raise ConfigError("s3.bucket contains invalid characters")
    if style == "virtual" and not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
        raise ConfigError(
            "virtual-hosted s3.bucket must be a lowercase DNS label without dots"
        )
    if ".." in bucket:
        raise ConfigError("s3.bucket must not contain adjacent dots")
    if not re.fullmatch(r"[A-Za-z0-9-]+", region):
        raise ConfigError("s3.region contains invalid characters")
    if not re.fullmatch(r"[A-Za-z0-9]+", access_key_id):
        raise ConfigError("s3.access_key_id contains invalid characters")
    if session_token is not None and (not isinstance(session_token, str) or not session_token):
        raise ConfigError("s3.session_token must be a non-empty string when set")

    return S3Config(
        endpoint=endpoint,
        bucket=bucket,
        region=region,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        session_token=session_token,
        prefix=prefix,
        addressing_style=style,
        url_ttl_seconds=ttl,
        put_headers=headers,
    )


def load_server_config(filename: str) -> ServerConfig:
    config_path = Path(filename).expanduser().resolve()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = _expand_env(json.load(handle))
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read configuration: %s" % exc) from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a JSON object")

    base = config_path.parent
    tls = _require(raw, "tls", dict)
    auth = _require(raw, "auth", dict)
    roots_raw = _require(raw, "allowed_roots", list)
    if not roots_raw or not all(isinstance(item, str) and item for item in roots_raw):
        raise ConfigError("allowed_roots must be a non-empty array of paths")

    token_hash = auth.get("token_sha256")
    token = auth.get("token")
    if token_hash is not None:
        if not isinstance(token_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", token_hash):
            raise ConfigError("auth.token_sha256 must be a 64-character hex SHA-256 digest")
        token_hash = token_hash.lower()
    elif isinstance(token, str) and token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    else:
        raise ConfigError("set auth.token_sha256 or auth.token")

    port = _positive_int(raw, "port", 9443)
    if port > 65535:
        raise ConfigError("port must be at most 65535")
    chunk_size = _positive_int(raw, "chunk_size", 64 * 1024 * 1024)
    if chunk_size < 5 * 1024 * 1024 or chunk_size > 256 * 1024 * 1024:
        raise ConfigError("chunk_size must be between 5 MiB and 256 MiB")

    listen = raw.get("listen", "0.0.0.0")
    state_dir = raw.get("state_dir", "./state")
    if not isinstance(listen, str) or not listen:
        raise ConfigError("listen must be a non-empty string")
    if not isinstance(state_dir, str) or not state_dir:
        raise ConfigError("state_dir must be a non-empty path string")

    return ServerConfig(
        listen=listen,
        port=port,
        cert_file=_path_from_config(_require(tls, "cert_file", str), base),
        key_file=_path_from_config(_require(tls, "key_file", str), base),
        auth_token_sha256=token_hash,
        allowed_roots=[_path_from_config(item, base) for item in roots_raw],
        state_dir=_path_from_config(state_dir, base),
        chunk_size=chunk_size,
        transfer_ttl_seconds=_positive_int(raw, "transfer_ttl_seconds", 24 * 3600),
        max_file_size=_positive_int(raw, "max_file_size", 1024 * 1024 * 1024 * 1024),
        s3=_load_s3(_require(raw, "s3", dict)),
    )
