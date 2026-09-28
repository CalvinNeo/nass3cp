"""Optional nathole process control, peer enrollment and local credential profiles."""

import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from .config import NatholeConfig
from .errors import ConfigError, Nass3cpError


LOG = logging.getLogger("nass3cp.nathole")
KEY_FILES = ("ca.pem", "cert.pem", "key.pem", "secret.key")
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def remove_private_tree(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    if path.is_symlink() or resolved.parent != parent.resolve():
        raise ConfigError("refusing to remove a credential directory outside its parent")
    shutil.rmtree(str(resolved))


def lock_profile(folder: Path) -> Any:
    handle = (folder / "session.lock").open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError as exc:
        handle.close()
        raise Nass3cpError("this NAS nathole profile is already active in another browser session") from exc


def private_directory(path: Path) -> None:
    """Create an owner-only directory, including a real Windows DACL."""
    if path.is_symlink():
        raise ConfigError("credential directories must not be symbolic links")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "nt":
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, timeout=10,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        match = re.search(r"S-1-5-[0-9-]+", identity.stdout)
        if identity.returncode or not match:
            raise ConfigError("cannot determine the current user for credential permissions")
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                                 "*%s:(OI)(CI)F" % match.group(), "*S-1-5-18:(OI)(CI)F"],
                                capture_output=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise ConfigError("cannot protect the credential directory")
    else:
        path.chmod(0o700)


def write_private(path: Path, data: str) -> None:
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    try:
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_program(program: Path) -> None:
    if not program.is_file() or not program.with_name("nat4_service.py").is_file():
        raise ConfigError("nathole is enabled but its service is unavailable; initialize third_party/nathole "
                          "or configure nathole.program")


def validate_keys(folder: Path, server: bool) -> None:
    try:
        secret = bytes.fromhex((folder / "secret.key").read_text(encoding="ascii").strip())
        if len(secret) != 32:
            raise ValueError("wrong secret length")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=str(folder / "ca.pem"))
        context.load_cert_chain(str(folder / "cert.pem"), str(folder / "key.pem"))
    except (OSError, ValueError) as exc:
        raise ConfigError("invalid nathole credential bundle") from exc


class ServiceProcess:
    """Own one nathole daemon; its independent supervisor owns the tunnel workers."""

    def __init__(self, program: Path, config_file: Path):
        self.program, self.config_file = program, config_file
        self.process: Any = None
        self.closed = threading.Event()
        self.condition = threading.Condition(threading.RLock())
        self.started = False
        self.peers: Dict[str, str] = {}
        self.responses: Dict[str, Any] = {}
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        validate_program(self.program)
        self.thread = threading.Thread(target=self._monitor, daemon=True, name="nathole-service")
        self.thread.start()
        deadline = time.monotonic() + 10
        with self.condition:
            while not self.started and not self.closed.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            started = self.started
        if not started:
            self.close()
            raise Nass3cpError("nathole service did not start; check its program and credential configuration")

    @staticmethod
    def _log(stream: Any) -> None:
        for line in stream:
            LOG.info("%s", line.rstrip())

    def _monitor(self) -> None:
        delay = 1.0
        while not self.closed.is_set():
            process = None
            reader = None
            try:
                process = subprocess.Popen(
                    [sys.executable, "-B", "-u", str(self.program), "daemon", "--config",
                     str(self.config_file), "--control-stdio"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                with self.condition:
                    self.process = process
                    if self.closed.is_set():
                        # Shutdown may arrive while Popen is still creating the child.
                        # The finally block closes its control pipe and reaps it.
                        continue
                reader = threading.Thread(target=self._log, args=(process.stderr,), daemon=True)
                reader.start()
                for line in process.stdout:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(value, dict) or value.get("version") != 1:
                        continue
                    with self.condition:
                        event = value.get("event")
                        if event == "started":
                            self.started = True
                        elif event == "peer_ready":
                            self.peers[value["name"]] = value["endpoint"]
                        elif event in ("peer_retry", "peer_removed", "peer_starting"):
                            self.peers.pop(value.get("name"), None)
                        request_id = value.get("id")
                        if request_id in self.responses:
                            self.responses[request_id] = value
                        self.condition.notify_all()
            except OSError:
                LOG.error("could not launch the nathole service")
            finally:
                with self.condition:
                    self.started = False
                    self.peers.clear()
                    self.condition.notify_all()
                if process is not None:
                    try:
                        process.stdin.close()
                        process.wait(timeout=8)
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
                        process.wait(timeout=5)
                    process.stdout.close()
                    if reader:
                        reader.join(timeout=1)
                    process.stderr.close()
            if not self.closed.is_set():
                LOG.warning("nathole service exited; retrying in %.0f seconds", delay)
                self.closed.wait(delay)
                delay = min(30, delay * 2)

    def reload(self) -> None:
        identifier = secrets.token_hex(8)
        deadline = time.monotonic() + 10
        with self.condition:
            while not self.started and not self.closed.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Nass3cpError("nathole service is unavailable")
                self.condition.wait(remaining)
            if self.closed.is_set():
                raise Nass3cpError("nathole service is closing")
            self.responses[identifier] = None
            try:
                self.process.stdin.write(json.dumps({"id": identifier, "command": "reload"}) + "\n")
                self.process.stdin.flush()
                while self.responses[identifier] is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self.started:
                        raise Nass3cpError("nathole did not acknowledge the updated peers")
                    self.condition.wait(remaining)
                if self.responses[identifier].get("event") != "reloaded":
                    raise Nass3cpError("nathole rejected the updated peers")
            finally:
                self.responses.pop(identifier, None)

    def wait_peer(self, name: str, cancel: threading.Event, timeout: float = 150) -> str:
        deadline = time.monotonic() + timeout
        with self.condition:
            while name not in self.peers:
                if cancel.is_set() or self.closed.is_set():
                    raise Nass3cpError("nathole connection cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Nass3cpError("nathole could not establish a direct connection; retry or choose S3")
                self.condition.wait(min(remaining, 0.2))
            return self.peers[name]

    def close(self) -> None:
        self.closed.set()
        with self.condition:
            process = self.process
            if process is not None and process.poll() is None:
                try:
                    process.stdin.write('{"command":"stop"}\n')
                    process.stdin.flush()
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=12)
            if self.thread.is_alive() and process is not None and process.poll() is None:
                process.kill()
                self.thread.join(timeout=5)


class ServerTunnels:
    def __init__(self, config: NatholeConfig, state_dir: Path, target: str):
        self.config, self.target = config, target
        self.directory = state_dir / "nathole"
        private_directory(self.directory)
        self.peer_directory = self.directory / "peers"
        private_directory(self.peer_directory)
        identity = self.directory / "identity"
        if not identity.exists():
            write_private(identity, secrets.token_hex(16))
        self.identity = identity.read_text(encoding="ascii").strip()
        if not ID_PATTERN.fullmatch(self.identity):
            raise ConfigError("invalid persisted nathole NAS identity")
        self.lock = threading.RLock()
        self.config_file = self.directory / "service.json"
        self.service = ServiceProcess(config.program, self.config_file)

    def _configuration(self) -> Dict[str, Any]:
        peers = []
        if self.config.keys_dir:
            peers.append({"name": "imported", "keys": str(self.config.keys_dir), "room": self.config.room})
        for folder in sorted(self.peer_directory.iterdir()):
            if folder.is_dir() and ID_PATTERN.fullmatch(folder.name):
                peers.append({"name": folder.name, "keys": str(folder), "room": self.room(folder.name)})
        if len(peers) > 32:
            raise ConfigError("at most 32 nathole peers are supported")
        for peer in peers:
            peer.update(role="serve", server=self.config.server, id="nas", target=self.target)
        return {"version": 1, "peers": peers}

    def room(self, identifier: str) -> str:
        return "nass3cp-" + self.identity + "-" + identifier

    def start(self) -> None:
        with self.lock:
            write_private(self.config_file, json.dumps(self._configuration()))
            self.service.start()

    def info(self) -> Dict[str, Any]:
        value = {"server_id": self.identity, "coordinator": self.config.server}
        if self.config.keys_dir is not None:
            value["imported_room"] = self.config.room
        return value

    def register(self, body: Dict[str, Any]) -> Dict[str, Any]:
        identifier, bundle = body.get("id"), body.get("credentials")
        if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier):
            raise Nass3cpError("invalid device identifier")
        if not isinstance(bundle, dict) or set(bundle) != set(KEY_FILES):
            raise Nass3cpError("a registration requires the four NAS credential files")
        for value in bundle.values():
            if not isinstance(value, str) or not 1 <= len(value) <= 16384 or "\x00" in value:
                raise Nass3cpError("invalid credential file")
            try:
                value.encode("ascii")
            except UnicodeEncodeError as exc:
                raise Nass3cpError("credential files must use ASCII") from exc
        with self.lock:
            folder = self.peer_directory / identifier
            if folder.exists():
                if any((folder / name).read_text(encoding="ascii") != value for name, value in bundle.items()):
                    raise Nass3cpError("this device identifier is already registered with different credentials")
            else:
                if len(self._configuration()["peers"]) >= 32:
                    raise Nass3cpError("the nathole device limit has been reached")
                pending = self.peer_directory / ("pending-" + secrets.token_hex(16))
                private_directory(pending)
                try:
                    for name, value in bundle.items():
                        write_private(pending / name, value)
                    validate_keys(pending, True)
                    pending.rename(folder)
                finally:
                    if pending.exists():
                        remove_private_tree(pending, self.peer_directory)
            write_private(self.config_file, json.dumps(self._configuration()))
            self.service.reload()
            return dict(self.info(), id=identifier, room=self.room(identifier))

    def remove(self, identifier: str) -> None:
        if not ID_PATTERN.fullmatch(identifier):
            raise Nass3cpError("invalid device identifier")
        with self.lock:
            folder = self.peer_directory / identifier
            if not folder.is_dir():
                raise Nass3cpError("device is not registered")
            retired = self.peer_directory / ("removed-" + identifier)
            folder.rename(retired)
            try:
                write_private(self.config_file, json.dumps(self._configuration()))
                self.service.reload()
            except Exception:
                retired.rename(folder)
                write_private(self.config_file, json.dumps(self._configuration()))
                raise
            remove_private_tree(retired, self.peer_directory)

    def close(self) -> None:
        self.service.close()


def client_state_directory() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "nass3cp" / "nathole"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "nass3cp" / "nathole"


class ClientTunnel:
    def __init__(self, api: Any, info: Dict[str, Any], program: Optional[Path] = None):
        self.api, self.info = api, info
        configured_program = os.environ.get("NASS3CP_NATHOLE_PROGRAM")
        self.program = program or (Path(configured_program).expanduser().resolve() if configured_program else
                                   Path(__file__).resolve().parents[2] / "third_party" / "nathole" / "nat4_tunnel.py")
        self.service: Optional[ServiceProcess] = None
        self.profile_lock: Any = None
        self.closed = threading.Event()
        self.lock = threading.Lock()

    def _connect(self) -> None:
        imported = os.environ.get("NASS3CP_NATHOLE_KEYS")
        if not imported and not self.api.trusted_connection:
            raise Nass3cpError("nathole registration requires verified HTTPS or an explicitly trusted encrypted tunnel")
        validate_program(self.program)
        identity = self.info.get("server_id")
        if not isinstance(identity, str) or not ID_PATTERN.fullmatch(identity):
            raise Nass3cpError("invalid NAS nathole identity")
        # A trust identity is bound to the original authenticated NAS endpoint.
        endpoint = hashlib.sha256(self.api.base_url.encode("utf-8")).hexdigest()[:16]
        folder = client_state_directory() / (endpoint + "-" + identity)
        private_directory(folder)
        self.profile_lock = lock_profile(folder)
        profile_file = folder / "profile.json"
        if imported:
            keys = Path(imported).expanduser().resolve()
            validate_keys(keys, False)
            if not self.info.get("imported_room"):
                raise Nass3cpError("the NAS has no imported nathole credentials configured")
            profile = {"room": self.info["imported_room"], "pending": False}
        elif profile_file.exists():
            profile = json.loads(profile_file.read_text(encoding="utf-8"))
            validate_keys(folder / "client", False)
        else:
            with tempfile.TemporaryDirectory(prefix="pair-", dir=str(folder)) as temporary:
                generated = Path(temporary) / "keys"
                result = subprocess.run([sys.executable, "-B", str(self.program), "keygen", "--out", str(generated)],
                                        capture_output=True, timeout=90,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                if result.returncode:
                    raise Nass3cpError("could not generate nathole credentials; install OpenSSL on this client")
                identifier = secrets.token_hex(16)
                bundle = {name: (generated / "nas" / name).read_text(encoding="ascii") for name in KEY_FILES}
                # Keep pending material so a lost registration response can be retried idempotently.
                for role in ("client", "nas"):
                    destination = folder / role
                    private_directory(destination)
                    for name in KEY_FILES:
                        write_private(destination / name, (generated / role / name).read_text(encoding="ascii"))
                profile = {"id": identifier, "pending": True}
                write_private(profile_file, json.dumps(profile))
        if not imported:
            keys = folder / "client"
        if profile.get("pending"):
            bundle = {name: (folder / "nas" / name).read_text(encoding="ascii") for name in KEY_FILES}
            registered = self.api.request("POST", "/v1/nathole/peers", {"id": profile["id"], "credentials": bundle})
            profile = {"id": profile["id"], "room": registered["room"], "pending": False}
            write_private(profile_file, json.dumps(profile))
            remove_private_tree(folder / "nas", folder)
        config_file = folder / ("session-" + secrets.token_hex(8) + ".json")
        configuration = {"version": 1, "peers": [{"name": "nas", "role": "connect",
                         "server": self.info["coordinator"], "room": profile["room"], "id": "client",
                         "keys": str(keys), "listen": "127.0.0.1:0"}]}
        write_private(config_file, json.dumps(configuration))
        self.service = ServiceProcess(self.program, config_file)
        try:
            self.service.start()
        except Exception:
            self.service = None
            config_file.unlink()
            raise

    def endpoint(self, cancel: threading.Event) -> Any:
        from .client import ApiClient
        with self.lock:
            if self.closed.is_set():
                raise Nass3cpError("nathole client is closing")
            if self.service is None:
                try:
                    self._connect()
                except Exception:
                    if self.profile_lock:
                        self.profile_lock.close()
                        self.profile_lock = None
                    raise
            endpoint = self.service.wait_peer("nas", cancel)
        host, port = endpoint.rsplit(":", 1)
        if not ipaddress.IPv4Address(host).is_loopback or not 1 <= int(port) <= 65535:
            raise Nass3cpError("invalid local nathole endpoint")
        api = ApiClient("http://" + endpoint, self.api.password, trusted_tunnel=True)
        # Local tunnel traffic must never be sent through environment HTTP proxies.
        from urllib.request import ProxyHandler, build_opener
        from .net import NoRedirectHandler
        api.opener = build_opener(ProxyHandler({}), NoRedirectHandler())
        return api

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            if self.service:
                self.service.close()
                try:
                    self.service.config_file.unlink()
                except FileNotFoundError:
                    pass
            if self.profile_lock:
                self.profile_lock.close()
                self.profile_lock = None
