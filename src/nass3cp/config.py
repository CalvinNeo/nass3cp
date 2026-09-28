import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .errors import ConfigError


_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_environment_file(filename: str) -> None:
    """Load a small, non-executable KEY=VALUE file without shell evaluation.

    Existing process environment variables take precedence over file values.
    Whole-line comments, optional ``export``, and single/double quoted values
    are supported. Variable expansion and command substitution are deliberately
    not supported.
    """
    path = Path(filename).expanduser().resolve()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError("cannot read environment file %s: %s" % (path, exc)) from exc

    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _ENV_NAME_PATTERN.fullmatch(name):
            raise ConfigError(
                "invalid environment assignment in %s at line %d" % (path, line_number)
            )
        value = raw_value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ConfigError(
                    "unterminated quoted value in %s at line %d" % (path, line_number)
                )
            if quote == "'":
                value = value[1:-1]
            else:
                try:
                    decoded = json.loads(value)
                except ValueError as exc:
                    raise ConfigError(
                        "invalid quoted value in %s at line %d" % (path, line_number)
                    ) from exc
                if not isinstance(decoded, str):
                    raise ConfigError(
                        "invalid quoted value in %s at line %d" % (path, line_number)
                    )
                value = decoded
        elif value.endswith(("'", '"')):
            raise ConfigError(
                "unmatched quote in %s at line %d" % (path, line_number)
            )
        if "\x00" in value:
            raise ConfigError(
                "NUL byte in environment value in %s at line %d" % (path, line_number)
            )
        os.environ.setdefault(name, value)


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
    presign_unsigned_payload: bool = False


@dataclass(frozen=True)
class SearchConfig:
    entries_per_second: int = 50
    max_results: int = 1000
    regex_timeout_ms: int = 100
    batch_size: int = 200
    max_pending_dirs: int = 2048
    exclude_dirs: Tuple[str, ...] = ()


def _load_search(raw: Any) -> SearchConfig:
    if not isinstance(raw, dict):
        raise ConfigError("search must be an object")
    values = {}
    for name, default, maximum in (
        ("entries_per_second", 50, 10000),
        ("max_results", 1000, 10000),
        ("regex_timeout_ms", 100, 5000),
        ("batch_size", 200, 10000),
        ("max_pending_dirs", 2048, 16384),
    ):
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ConfigError("search.%s must be an integer between 1 and %d" % (name, maximum))
        values[name] = value
    excluded = raw.get("exclude_dirs", [])
    if not isinstance(excluded, list) or len(excluded) > 256:
        raise ConfigError("search.exclude_dirs must be a list of up to 256 folder names")
    for name in excluded:
        if (not isinstance(name, str) or not 1 <= len(name) <= 255 or name in (".", "..")
                or any(character in name for character in ("/", "\\", "\x00"))):
            raise ConfigError("search.exclude_dirs must contain folder names, not paths or patterns")
    values["exclude_dirs"] = tuple(dict.fromkeys(excluded))
    return SearchConfig(**values)


@dataclass(frozen=True)
class NatholeConfig:
    server: str
    program: Path
    keys_dir: Optional[Path] = None
    room: str = "nass3cp"
    trusted_http_registration: bool = False


def _load_nathole(raw: Any, base: Path) -> Optional[NatholeConfig]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("nathole must be an object")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("nathole.enabled must be a boolean")
    if not enabled:
        return None
    raw = _expand_env(raw)
    coordinator = _require(raw, "coordinator", dict)
    try:
        host = str(ipaddress.IPv4Address(_require(coordinator, "host", str)))
    except ValueError as exc:
        raise ConfigError("nathole.coordinator.host must be an IPv4 address") from exc
    port = _positive_int(coordinator, "port", 40000)
    if port > 65533:
        raise ConfigError("nathole.coordinator.port must be at most 65533 (UDP uses port + 1 and + 2)")
    program_value = raw.get("program")
    if program_value is None:
        program = Path(__file__).resolve().parents[2] / "third_party" / "nathole" / "nat4_tunnel.py"
    elif isinstance(program_value, str) and program_value:
        program = _path_from_config(program_value, base)
    else:
        raise ConfigError("nathole.program must be a path to nat4_tunnel.py")
    keys = raw.get("keys_dir")
    if keys is not None and (not isinstance(keys, str) or not keys):
        raise ConfigError("nathole.keys_dir must be a path")
    room = raw.get("room", "nass3cp")
    if not isinstance(room, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", room):
        raise ConfigError("nathole.room must be a label of 1 to 64 letters, digits, underscores or hyphens")
    trusted = raw.get("trusted_http_registration", False)
    if not isinstance(trusted, bool):
        raise ConfigError("nathole.trusted_http_registration must be a boolean")
    return NatholeConfig("%s:%d" % (host, port), program,
                         _path_from_config(keys, base) if keys else None, room, trusted)


@dataclass(frozen=True)
class ServerConfig:
    listen: str
    port: int
    tls_enabled: bool
    cert_file: Optional[Path]
    key_file: Optional[Path]
    auth_password_sha256: str
    allowed_roots: List[Path]
    state_dir: Path
    chunk_size: int
    transfer_ttl_seconds: int
    max_file_size: int
    s3: Optional[S3Config]
    search: SearchConfig = field(default_factory=SearchConfig)
    nathole: Optional[NatholeConfig] = None


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

    presign_unsigned_payload = raw.get("presign_unsigned_payload", False)
    if not isinstance(presign_unsigned_payload, bool):
        raise ConfigError("s3.presign_unsigned_payload must be a boolean")

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
        presign_unsigned_payload=presign_unsigned_payload,
    )


def load_server_config(filename: str) -> ServerConfig:
    config_path = Path(filename).expanduser().resolve()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read configuration: %s" % exc) from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a JSON object")

    base = config_path.parent
    nathole = _load_nathole(raw.get("nathole"), base)
    # An inactive optional integration must not demand its secrets or paths.
    raw = _expand_env({key: value for key, value in raw.items() if key != "nathole"})
    tls = _require(raw, "tls", dict)
    auth = _require(raw, "auth", dict)
    roots_raw = _require(raw, "allowed_roots", list)
    if not roots_raw or not all(isinstance(item, str) and item for item in roots_raw):
        raise ConfigError("allowed_roots must be a non-empty array of paths")

    auth_options = (
        "password_sha256",
        "password",
        "token_sha256",  # Backward-compatible names used by version 0.1.
        "token",
    )
    configured_auth = [name for name in auth_options if auth.get(name) is not None]
    if len(configured_auth) != 1:
        raise ConfigError("set exactly one auth.password or auth.password_sha256")
    auth_name = configured_auth[0]
    auth_value = auth[auth_name]
    if auth_name.endswith("_sha256"):
        if not isinstance(auth_value, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", auth_value
        ):
            raise ConfigError(
                "auth.%s must be a 64-character hex SHA-256 digest" % auth_name
            )
        password_hash = auth_value.lower()
    elif isinstance(auth_value, str) and auth_value:
        if any(character in auth_value for character in ("\r", "\n", "\x00")):
            raise ConfigError("auth.%s must be a single line" % auth_name)
        password_hash = hashlib.sha256(auth_value.encode("utf-8")).hexdigest()
    else:
        raise ConfigError("auth.%s must be a non-empty string" % auth_name)

    tls_enabled = tls.get("enabled", True)
    if not isinstance(tls_enabled, bool):
        raise ConfigError("tls.enabled must be a boolean")
    cert_file: Optional[Path] = None
    key_file: Optional[Path] = None
    if tls_enabled:
        cert_file = _path_from_config(_require(tls, "cert_file", str), base)
        key_file = _path_from_config(_require(tls, "key_file", str), base)

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
        tls_enabled=tls_enabled,
        cert_file=cert_file,
        key_file=key_file,
        auth_password_sha256=password_hash,
        allowed_roots=[_path_from_config(item, base) for item in roots_raw],
        state_dir=_path_from_config(state_dir, base),
        chunk_size=chunk_size,
        transfer_ttl_seconds=_positive_int(raw, "transfer_ttl_seconds", 24 * 3600),
        max_file_size=_positive_int(raw, "max_file_size", 1024 * 1024 * 1024 * 1024),
        s3=(_load_s3(_require(raw, "s3", dict))
            if raw.get("s3") is not None or nathole is None else None),
        search=_load_search(raw.get("search", {})),
        nathole=nathole,
    )
