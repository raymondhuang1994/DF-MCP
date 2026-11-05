"""One managed local index owner, shared by independent JSON-only MCP bridges.

No network listener, pickle deserialization, or source writes. A guarded OS
lease elects the owner; IPC credentials live in the same private state boundary.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import threading
import time
from multiprocessing.connection import Listener

from .engine import Engine, failure
from .errors import DriveError
from .fs_access import SourceAccess, StateAccess, validate_state_boundaries
from .service_config import PROTOCOL_VERSION, SERVICE_VERSION, settings

MAX_MESSAGE = 262144


def receive(connection, timeout=60):
    if not connection.poll(timeout):
        raise TimeoutError()
    value = json.loads(connection.recv_bytes(MAX_MESSAGE).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError()
    return value


def send(connection, value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE:
        raise ValueError()
    connection.send_bytes(payload)


def socket_location(config, source, installation):
    if os.name == "nt":
        return r"\\.\pipe\df-mcp-" + secrets.token_hex(20), "AF_PIPE", None
    # Keep below the shortest AF_UNIX path limit, even with long source/config paths.
    directory = Path("/tmp").resolve() / ("df-mcp-%d" % os.getuid())
    validate_state_boundaries(directory, source, installation)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DriveError("UNSAFE_STATE", "Local IPC storage must be an owner-only ordinary directory.")
    guard = StateAccess(directory, source, installation)
    name = hashlib.sha256(str(config["base"].resolve()).encode()).hexdigest()[:24] + ".sock"
    address = directory / name
    try:
        existing = address.lstat()
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
            raise DriveError("UNSAFE_STATE", "The local IPC endpoint was replaced by an unexpected object.")
        address.unlink()  # Only the elected owner may clear a stale socket.
    except FileNotFoundError:
        pass
    return str(address), "AF_UNIX", guard


class Broker:
    def __init__(self, config_path):
        self.config = settings(config_path)
        self.stop_event = threading.Event()
        self.clients_lock = threading.Lock()
        self.call_lock = threading.Lock()
        self.connections = set()
        self.last_empty = time.monotonic()
        self.instance_id = secrets.token_hex(16)
        self.token = secrets.token_hex(32)
        self.source = self.control = self.engine = self.listener = self.socket_guard = None
        self.elected = False
        try:
            self.source = (SourceAccess(self.config["root"], allow_network=True)
                           if self.config["config"].get("provider") == "windows_share"
                           else SourceAccess(self.config["root"]))
            install = Path(__file__).resolve().parents[2]
            base = validate_state_boundaries(self.config["base"], self.source, install)
            self.control = StateAccess(base / "shared-v1" / "control", self.source, install)
            self.control.lock()
            self.elected = True
            # Publish no socket until initialization and recovery have succeeded.
            self.engine = Engine(str(self.config["path"]), state_override=base / "shared-v1" / "index", config_snapshot=self.config["config"])
            address, family, self.socket_guard = socket_location(self.config, self.source, install)
            self.listener = Listener(address, family=family, backlog=32)
            if family == "AF_UNIX":
                os.chmod(address, 0o600)
            self.endpoint = {"protocol": PROTOCOL_VERSION, "service_version": SERVICE_VERSION,
                             "instance_id": self.instance_id, "config_id": self.config["config_id"],
                             "code_id": self.config["code_id"], "family": family, "address": address,
                             "token": self.token, "pid": os.getpid()}
            self.control.write_metadata("endpoint.json", self.endpoint)
            self.control.remove_metadata("startup-error.json")
            self.last_empty = time.monotonic()
        except Exception as exc:
            if self.elected:
                try:
                    result = failure(exc.code, exc.message, exc.retryable, exc.next_action) if isinstance(exc, DriveError) else failure("SERVICE_START_FAILED", "The shared index could not start.")
                    self.control.write_metadata("startup-error.json", {"config_id": self.config["config_id"], "code_id": self.config["code_id"], "time": time.time(), "result": result})
                except Exception:
                    pass
            self.close()
            raise

    def handle(self, connection):
        session = secrets.token_hex(16)
        try:
            hello = receive(connection, 5)
            if hello.get("type") != "hello" or not isinstance(hello.get("token"), str) or not hmac.compare_digest(hello["token"], self.token):
                send(connection, failure("SERVICE_AUTH_FAILED", "The local service credential is invalid."))
                return
            if os.name == "nt":
                from .windows_identity import require_same_user_pipe
                try:
                    require_same_user_pipe(connection.fileno())
                except DriveError as exc:
                    send(connection, failure(exc.code, exc.message, exc.retryable, exc.next_action))
                    return
            if hello.get("config_id") != self.config["config_id"] or hello.get("code_id") != self.config["code_id"]:
                send(connection, failure("SERVICE_CONFIGURATION_CONFLICT", "An older or differently configured service is still active.", False, "Close connections using the earlier configuration, wait for idle shutdown, then reconnect."))
                return
            send(connection, {"ok": True, "session_id": session, "instance_id": self.instance_id})
            while not self.stop_event.is_set():
                try:
                    request = receive(connection, 1)
                except TimeoutError:
                    continue
                request_id = request.get("id")
                if isinstance(request_id, (dict, list)):
                    raise ValueError()
                if request.get("type") == "close":
                    return
                if not self.call_lock.acquire(timeout=45):
                    result = failure("SERVICE_BUSY", "Another local operation is still running.", True, "Wait for that operation to finish before retrying.")
                else:
                    try:
                        result = self.engine.call(request.get("method"), request.get("params", {}), session_id=session)
                        if request.get("method") == "status" and result.get("ok"):
                            with self.clients_lock:
                                active = len(self.connections)
                            result.update(service_version=SERVICE_VERSION, service_protocol=PROTOCOL_VERSION,
                                          instance_id=self.instance_id, config_id=self.config["config_id"],
                                          source_id=self.engine._root_binding,
                                          state_storage={**result.get("state_storage", {}), "mode": "shared", "persistent": True, "index_copies": 1,
                                                         "active_clients": active, "max_clients": self.config["max_clients"],
                                                         "idle_seconds": self.config["idle_seconds"],
                                                         "recovery_policy": "last_completed_checkpoint"})
                    finally:
                        self.call_lock.release()
                send(connection, {"id": request_id, "result": result})
                if result.get("error", {}).get("code") == "INDEXER_UNHEALTHY":
                    self.stop_event.set()
        except (EOFError, OSError, ValueError, TypeError, TimeoutError):
            pass
        finally:
            connection.close()
            with self.clients_lock:
                self.connections.discard(connection)
                if not self.connections:
                    self.last_empty = time.monotonic()

    def accept(self):
        while not self.stop_event.is_set():
            try:
                connection = self.listener.accept()
            except (OSError, ValueError, TypeError):
                break
            with self.clients_lock:
                # Idle shutdown and admission share one decision boundary. An
                # accept that finished after shutdown began must not revive it.
                if self.stop_event.is_set():
                    connection.close()
                    break
                if len(self.connections) >= self.config["max_clients"]:
                    # This bounded reply contains no private status or source information.
                    try:
                        send(connection, failure("SERVICE_CAPACITY", "The local connection limit is reached.", True, "Close an unused connection and retry."))
                    except (OSError, ValueError):
                        pass
                    connection.close()
                    continue
                self.connections.add(connection)
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()

    def run(self):
        thread = threading.Thread(target=self.accept, daemon=True)
        thread.start()
        try:
            while not self.stop_event.wait(0.25):
                with self.clients_lock:
                    idle = not self.connections and time.monotonic() - self.last_empty >= self.config["idle_seconds"]
                    if idle:
                        self.stop_event.set()
                if idle:
                    break
        finally:
            self.close()

    def close(self):
        self.stop_event.set()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        with self.clients_lock:
            for connection in tuple(self.connections):
                connection.close()
        if self.engine is not None:
            self.engine.close()
            self.engine = None
        if self.control is not None:
            if self.elected:
                try:
                    self.control.remove_metadata("endpoint.json")
                except (DriveError, OSError):
                    pass
            self.control.close()
            self.control = None
        if self.socket_guard is not None:
            self.socket_guard.close()
            self.socket_guard = None
        if self.source is not None:
            self.source.close()
            self.source = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    broker = None
    try:
        broker = Broker(args.config)
        def terminate(signum, frame):
            broker.stop_event.set()
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        broker.run()
        return 0
    except (DriveError, OSError, ValueError):
        return 1
    finally:
        if broker is not None:
            broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
