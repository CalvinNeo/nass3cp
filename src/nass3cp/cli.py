import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import __version__
from .client import ApiClient, download, upload
from .errors import Nass3cpError


def _remote(value: str) -> Optional[str]:
    if value.startswith("nas:"):
        return value[4:]
    return None


def _base_url(host: str, port: int, tls: bool = True) -> str:
    if (
        not host
        or "://" in host
        or "/" in host
        or "\\" in host
        or "?" in host
        or "#" in host
        or "@" in host
        or any(character.isspace() for character in host)
    ):
        raise Nass3cpError("--host must be a hostname or IP address, not a URL")
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = "[" + host + "]"
    return "%s://%s:%d" % ("https" if tls else "http", host, port)


def _password(args: argparse.Namespace) -> str:
    password_file = args.password_file or args.token_file
    if args.token is not None:
        value = args.token
    elif password_file is not None:
        try:
            value = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise Nass3cpError("cannot read password file: %s" % exc) from exc
    else:
        value = os.environ.get("NASS3CP_PASSWORD") or os.environ.get("NASS3CP_TOKEN", "")
        if not value:
            try:
                value = getpass.getpass("NAS password: ")
            except EOFError as exc:
                raise Nass3cpError(
                    "cannot read a password; use an interactive terminal or --password-file"
                ) from exc
    if not value:
        raise Nass3cpError("password must not be empty")
    if "\r" in value or "\n" in value:
        raise Nass3cpError("password must be a single line")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nass3cp",
        description="Copy one file between this computer and a NAS through S3",
    )
    parser.add_argument("--host", required=True, help="NAS service hostname or IP")
    parser.add_argument("--port", type=int, default=9443, help="NAS service port (default: 9443)")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--token", help=argparse.SUPPRESS)
    auth.add_argument("--token-file", help=argparse.SUPPRESS)
    auth.add_argument("--password-file", help="read the NAS password from this file instead of prompting")
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument("--ca-file", help="CA certificate used to verify the NAS service")
    tls.add_argument(
        "--insecure",
        action="store_true",
        help="encrypt but do not authenticate the NAS TLS certificate (unsafe)",
    )
    tls.add_argument(
        "--no-tls",
        action="store_true",
        help="use HTTP over an already-encrypted overlay network or tunnel",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing destination file")
    parser.add_argument("--jobs", type=int, default=2, help="parallel S3 requests (default: 2)")
    parser.add_argument(
        "--transfer-timeout",
        type=int,
        default=24 * 3600,
        help="seconds to wait for NAS-side work (default: 86400)",
    )
    parser.add_argument("--quiet", action="store_true", help="do not print progress")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("src", help="source path; prefix the NAS side with nas:")
    parser.add_argument("dst", help="destination path; prefix the NAS side with nas:")
    return parser


def _validate_args(args: argparse.Namespace) -> Tuple[Optional[str], Optional[str]]:
    if args.port <= 0 or args.port > 65535:
        raise Nass3cpError("--port must be between 1 and 65535")
    if args.jobs <= 0 or args.jobs > 16:
        raise Nass3cpError("--jobs must be between 1 and 16")
    if args.transfer_timeout <= 0:
        raise Nass3cpError("--transfer-timeout must be positive")
    source_remote = _remote(args.src)
    destination_remote = _remote(args.dst)
    if (source_remote is None) == (destination_remote is None):
        raise Nass3cpError("exactly one of src and dst must start with nas:")
    if source_remote == "" or destination_remote == "":
        raise Nass3cpError("NAS path after nas: must not be empty")
    return source_remote, destination_remote


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        source_remote, destination_remote = _validate_args(args)
        if args.insecure:
            print("warning: --insecure permits a man-in-the-middle attack", file=sys.stderr)
        if args.no_tls:
            print(
                "warning: NAS control traffic relies on the overlay/tunnel for encryption",
                file=sys.stderr,
            )
        api = ApiClient(
            _base_url(args.host, args.port, tls=not args.no_tls),
            _password(args),
            ca_file=args.ca_file,
            insecure=args.insecure,
        )
        if source_remote is None:
            upload(
                api,
                args.src,
                str(destination_remote),
                args.overwrite,
                args.jobs,
                args.transfer_timeout,
                args.quiet,
            )
        else:
            download(
                api,
                source_remote,
                args.dst,
                args.overwrite,
                args.jobs,
                args.transfer_timeout,
                args.quiet,
            )
    except KeyboardInterrupt:
        print("nass3cp: cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (Nass3cpError, ValueError, OSError) as exc:
        print("nass3cp: %s" % exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
