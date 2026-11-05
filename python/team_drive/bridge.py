"""Lightweight local IPC client; no database ownership and no request replay."""
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from multiprocessing.connection import Client, Connection

from .broker import receive, send
from .errors import DriveError
from .fs_access import SourceAccess, StateAccess, validate_state_boundaries
from .service_config import PROTOCOL_VERSION, settings


def connect_endpoint(endpoint):
    if not isinstance(endpoint, dict) or endpoint.get("protocol") != PROTOCOL_VERSION:
        raise DriveError("SERVICE_PROTOCOL_MISMATCH", "The local service protocol differs. Close older connections and restart.")
    address = endpoint.get("address")
    token = endpoint.get("token")
    if not isinstance(address, str) or not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise DriveError("UNSAFE_STATE", "The local endpoint metadata is invalid.")
    if os.name == "nt":
        if re.fullmatch(r"\\\\\.\\pipe\\df-mcp-[0-9a-f]{40}", address) is None or endpoint.get("family") != "AF_PIPE":
            raise DriveError("UNSAFE_STATE", "Only the configured local named pipe is permitted.")
        return Client(address, family="AF_PIPE")
    expected = Path("/tmp").resolve() / ("df-mcp-%d" % os.getuid())
    if Path(address).parent != expected or re.fullmatch(r"[0-9a-f]{24}\.sock", Path(address).name) is None or endpoint.get("family") != "AF_UNIX":
        raise DriveError("UNSAFE_STATE", "Only the owner-private local socket is permitted.")
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        channel.settimeout(2)
        channel.connect(address)
        channel.setblocking(True)
        return Connection(channel.detach())
    finally:
        channel.close()


class Bridge:
    def __init__(self, config_path):
        self.config = settings(config_path)
        self.connection = None
        self.session_id = None
        self.instance_id = None
        self.counter = 0
        self.connect()

    def connect(self):
        source = storage = None
        spawned = None
        last_spawn = None
        started = time.time()
        try:
            source = SourceAccess(self.config["root"], allow_network=self.config.get("config", {}).get("provider", "local") == "windows_share")
            installation = Path(__file__).resolve().parents[2]
            base = validate_state_boundaries(self.config["base"], source, installation)
            storage = StateAccess(base / "shared-v1" / "control", source, installation)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                endpoint = storage.read_metadata("endpoint.json")
                if endpoint:
                    channel = None
                    try:
                        channel = connect_endpoint(endpoint)
                        send(channel, {"type": "hello", "token": endpoint["token"],
                                       "config_id": self.config["config_id"], "code_id": self.config["code_id"]})
                        hello = receive(channel, 5)
                        if hello.get("ok") is not True:
                            error = hello.get("error", {})
                            if error.get("code") == "SERVICE_AUTH_FAILED":
                                # A restarted Unix owner can rebind the same
                                # socket after we read its predecessor's token.
                                # Retry only a demonstrably changed protected
                                # endpoint, before any tool request is sent.
                                current = storage.read_metadata("endpoint.json")
                                if current and any(current.get(key) != endpoint.get(key) for key in ("instance_id", "token")):
                                    channel.close()
                                    channel = None
                                    continue
                            raise DriveError(error.get("code", "SERVICE_CONNECT_FAILED"), error.get("message", "The shared service rejected this connection."), error.get("retryable", False), error.get("next_action", "Check local service configuration."))
                        self.connection = channel
                        self.session_id, self.instance_id = hello["session_id"], hello["instance_id"]
                        return
                    except (OSError, EOFError, TimeoutError):
                        if channel is not None:
                            channel.close()
                    except Exception:
                        if channel is not None:
                            channel.close()
                        raise
                error = storage.read_metadata("startup-error.json")
                if error and error.get("config_id") == self.config["config_id"] and error.get("code_id") == self.config["code_id"] and error.get("time", 0) >= started:
                    detail = error.get("result", {}).get("error", {})
                    raise DriveError(detail.get("code", "SERVICE_START_FAILED"), detail.get("message", "The shared service could not start."), detail.get("retryable", False), detail.get("next_action", "Check the local setup."))
                # A contender can lose the lease to an owner already shutting
                # down. Retry an exited contender at most once per second,
                # within the same overall startup deadline; never replay calls.
                if spawned is None or (spawned.poll() is not None and time.monotonic() - last_spawn >= 1):
                    options = {"start_new_session": True} if os.name != "nt" else {
                        "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                    spawned = subprocess.Popen([sys.executable, "-m", "team_drive.broker", "--config", str(self.config["path"])],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               close_fds=True, **options)
                    last_spawn = time.monotonic()
                time.sleep(0.05)
            raise DriveError("SERVICE_START_TIMEOUT", "The shared local index did not become ready in time.", True,
                             "Retry shortly; check source access and local service health if this persists.")
        finally:
            # Reap a losing election process without waiting on the elected service.
            if spawned is not None:
                spawned.poll()
            if storage is not None:
                storage.close()
            if source is not None:
                source.close()

    def call(self, method, params):
        from .engine import failure
        if self.connection is None:
            self.connect()
        self.counter += 1
        request_id = self.counter
        try:
            send(self.connection, {"id": request_id, "method": method, "params": params})
            response = receive(self.connection, 300 if method == "evidence_bundle" else 55)
            if response.get("id") != request_id or not isinstance(response.get("result"), dict):
                raise ValueError()
            return response["result"]
        except TimeoutError:
            self.close()
            return failure("REQUEST_TIMEOUT", "The local operation timed out; it may still finish in the shared service.", False,
                           "Check status or job progress before retrying a refresh or export. The request was not replayed.")
        except (OSError, EOFError, ValueError, TypeError):
            self.close()
            return failure("BACKEND_UNAVAILABLE", "The shared local service disconnected.", True,
                           "Retry on this connection; restart pagination after reconnection. The request was not replayed.")

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
