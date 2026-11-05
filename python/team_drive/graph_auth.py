"""Explicit Microsoft device login and non-interactive delegated token refresh.

OAuth requests go only to Microsoft's public-cloud authority. Graph access
tokens are opaque and never decoded. Identity metadata is read only from an ID
token delivered directly by that authenticated TLS token endpoint for this
client; this is not a general-purpose JWT authentication or verification API.
Refresh credentials use private POSIX files or Windows current-user DPAPI.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .errors import DriveError
from .fs_access import validate_state_location
from .bounded_json import loads as bounded_json_loads


_AUTHORITY = "https://login.microsoftonline.com"
_CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
_READ_SCOPES = {value.casefold(): value for value in ("Files.Read", "Files.Read.All", "Sites.Read.All")}
_IDENTITY_SCOPES = {"openid", "profile", "offline_access"}
_MAX_BYTES = 262144
_RELOGIN = "Run the explicit login-cloud command for this profile; normal tool calls never open a login flow."


def _error(code, message, retryable=False, action=_RELOGIN):
    return DriveError(code, message, retryable, action)


def _uuid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise ValueError()
    return str(uuid.UUID(value))


def _scope(value):
    if not isinstance(value, str):
        raise ValueError()
    if value.startswith("https://graph.microsoft.com/"):
        value = value[len("https://graph.microsoft.com/"):]
    if value.casefold() in _READ_SCOPES:
        return _READ_SCOPES[value.casefold()]
    if value in _IDENTITY_SCOPES:
        return value
    raise ValueError()


def _response_scope(value):
    # Microsoft incremental consent may include a previously granted User.Read
    # in a Graph token. Tolerate that response-only compatibility scope without
    # requesting it or enabling any /me/profile API calls. All other additional
    # resource permissions, particularly write permissions, remain rejected.
    # https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/resources-and-scopes.md
    if isinstance(value, str) and value.removeprefix("https://graph.microsoft.com/").casefold() == "user.read":
        return "User.Read"
    return _scope(value)


def validate_auth(config):
    """Normalize the authentication subset of a graph configuration, with no I/O."""
    try:
        if not isinstance(config, dict):
            raise ValueError()
        tenant = config.get("tenant_id")
        if tenant not in ("common", "organizations", "consumers"):
            tenant = _uuid(tenant)
        client = _uuid(config.get("client_id"))
        scopes = config.get("scopes", ["Files.Read"])
        if not isinstance(scopes, list) or not 1 <= len(scopes) <= 6:
            raise ValueError()
        scopes = sorted({_scope(value) for value in scopes} - _IDENTITY_SCOPES)
        if not scopes:
            raise ValueError()
        token_env = config.get("token_env")
        if "token_env" in config and (not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", token_env)):
            raise ValueError()
        timeout = config.get("timeout_seconds", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 20 or not math.isfinite(timeout):
            raise ValueError()
        result = {"tenant_id": tenant, "client_id": client, "scopes": scopes, "timeout_seconds": float(timeout)}
        if token_env is not None:
            result["token_env"] = token_env
        return result
    except (ValueError, TypeError, OverflowError):
        raise _error("INVALID_CONFIG", "Graph authentication requires a tenant GUID or official alias, client GUID, read-only scopes, and bounded settings.") from None


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _credential(value):
    return isinstance(value, str) and 1 <= len(value) <= 131072 and all(33 <= ord(char) <= 126 for char in value)


class _OAuthError(Exception):
    def __init__(self, code):
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _error("AUTH_REDIRECT_BLOCKED", "The authorization endpoint attempted an unexpected redirect.")


def _request(tenant, action, fields, timeout):
    if action not in ("devicecode", "token"):
        raise _error("AUTH_INVALID_REQUEST", "Unsupported authorization operation.")
    # tenant is already a UUID or one of three fixed aliases, never a URL.
    url = _AUTHORITY + "/" + tenant + "/oauth2/v2.0/" + action
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    deadline = time.monotonic() + timeout
    response = None
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        chunks, size = [], 0
        while size <= _MAX_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _error("AUTH_UNAVAILABLE", "The authorization service timed out.", True)
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            chunk = response.read1(min(65536, _MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > _MAX_BYTES:
            raise ValueError()
        value = bounded_json_loads(b"".join(chunks))
        if not isinstance(value, dict):
            raise ValueError()
        if value.get("error"):
            code = value["error"]
            known = {"authorization_pending", "slow_down", "authorization_declined", "access_denied",
                     "expired_token", "bad_verification_code", "invalid_grant", "interaction_required",
                     "login_required", "consent_required", "invalid_client", "unauthorized_client",
                     "invalid_scope", "temporarily_unavailable", "server_error"}
            raise _OAuthError(code if isinstance(code, str) and code in known else "unknown")
        if response.status != 200:
            raise _error("AUTH_UNAVAILABLE", "The authorization service did not complete the request.", True)
        return value
    except DriveError:
        raise
    except _OAuthError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError):
        raise _error("AUTH_UNAVAILABLE", "The authorization service is unavailable.", True) from None
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise _error("AUTH_INVALID_RESPONSE", "The authorization service returned an invalid response.") from None
    finally:
        if response is not None:
            response.close()


def _oauth_failure(exc):
    if exc.code in {"invalid_grant", "interaction_required", "login_required", "consent_required"}:
        return _error("AUTH_REQUIRED", "The saved authorization expired, was revoked, or requires user interaction.")
    if exc.code in {"authorization_declined", "access_denied"}:
        return _error("AUTH_DECLINED", "The authorization request was declined.")
    if exc.code in {"expired_token", "bad_verification_code"}:
        return _error("AUTH_EXPIRED", "The device authorization request expired or is no longer valid.")
    if exc.code in {"temporarily_unavailable", "server_error"}:
        return _error("AUTH_UNAVAILABLE", "The authorization service is temporarily unavailable.", True)
    return _error("AUTH_FAILED", "Microsoft did not authorize this client and requested read scopes.")


def _dpapi(data, decrypt=False):
    """Current-user DPAPI; called only on Windows, never a plaintext fallback."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    raw = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        arguments = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        arguments = (ctypes.byref(source), "df-mcp Graph refresh credential", None, None, None, 1, ctypes.byref(output))
    function.restype = wintypes.BOOL
    if not function(*arguments):
        raise _error("AUTH_CACHE_UNSAFE", "The operating system could not protect or open this user's credential cache.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class _CacheDirectory:
    """Pin a dedicated credential directory; all file publication stays inside it."""
    def __init__(self, path, create=False):
        self.path = Path(path).expanduser().absolute()
        self.fd = None
        self.guard = None
        self.identities = []
        validate_state_location(self.path)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", self.path.name) or self.path.name in (".", ".."):
            raise _error("AUTH_CACHE_UNSAFE", "Use a simple credential-cache filename in a private profile directory.")
        try:
            if os.name == "nt":
                from . import fs_access as fs
                if not create and not self.path.parent.exists():
                    raise FileNotFoundError()
                self.guard = fs._WindowsDirectories(self.path.parent, create=create, code="AUTH_CACHE_UNSAFE", state=True)
                self.path = self.guard.path / self.path.name
            else:
                current = Path(self.path.anchor)
                self.fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                self.identities.append((current, os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino))
                for part in self.path.parent.parts[1:]:
                    if create:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=self.fd)
                        except FileExistsError:
                            pass
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                    os.close(self.fd)
                    self.fd = next_fd
                    current = current / part
                    info = os.fstat(self.fd)
                    self.identities.append((current, info.st_dev, info.st_ino))
                info = os.fstat(self.fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise _error("AUTH_CACHE_UNSAFE", "The credential directory must belong to this user and have mode 0700.")
            self.check()
        except Exception as exc:
            self.close()
            if isinstance(exc, (DriveError, FileNotFoundError)):
                raise
            raise _error("AUTH_CACHE_UNSAFE", "The credential directory is redirected or cannot be opened safely.") from None

    def check(self):
        if self.guard is not None:
            self.guard.check()
        else:
            for path, device, inode in self.identities:
                info = os.stat(path, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (device, inode):
                    raise _error("AUTH_CACHE_UNSAFE", "The credential directory changed during the operation.")

    def open(self, name):
        self.check()
        fd = None
        try:
            if os.name == "nt":
                from . import fs_access as fs
                handle = fs._handle(self.path.parent / name, metadata_read=True)
                try:
                    info = fs._file_info(handle)
                    if info.st_file_attributes & fs._REPARSE or fs._final_path(handle) != fs._canonical(self.path.parent / name):
                        raise _error("AUTH_CACHE_UNSAFE", "The credential file is redirected.")
                    fd = fs.msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                    handle = None
                finally:
                    if handle is not None:
                        fs._kernel.CloseHandle(handle)
            else:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise _error("AUTH_CACHE_UNSAFE", "The credential file must belong to this user and have mode 0600.")
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_BYTES:
                raise _error("AUTH_CACHE_UNSAFE", "The credential cache must be a bounded ordinary file with one hard link.")
            self.check()
            result, fd = fd, None
            return result
        finally:
            if fd is not None:
                os.close(fd)

    def read(self):
        try:
            fd = self.open(self.path.name)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as source:
            data = source.read(_MAX_BYTES + 1)
        self.check()
        if len(data) > _MAX_BYTES:
            raise _error("AUTH_CACHE_INVALID", "The credential cache is invalid.")
        if os.name == "nt":
            if not data.startswith(b"DFG1\0"):
                raise _error("AUTH_CACHE_UNSAFE", "This credential cache is not protected for the current Windows user.")
            data = _dpapi(data[5:], decrypt=True)
        try:
            value = bounded_json_loads(data)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise _error("AUTH_CACHE_INVALID", "The credential cache is invalid.") from None

    def write(self, value):
        data = _json(value).encode()
        if os.name == "nt":
            data = b"DFG1\0" + _dpapi(data)
        if len(data) > _MAX_BYTES:
            raise _error("AUTH_CACHE_INVALID", "The credential cache exceeds its bounded size.")
        # Refuse an unsafe existing destination even though atomic replacement
        # itself would not follow its symlink or overwrite a hard-linked target.
        try:
            fd = self.open(self.path.name)
            os.close(fd)
        except FileNotFoundError:
            pass
        temporary = ".auth-" + secrets.token_hex(12) + ".tmp"
        published = False
        try:
            if os.name == "nt":
                fd = os.open(self.path.parent / temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
            else:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            self.check()
            if os.name == "nt":
                from . import fs_access as fs
                handle = fs._handle(self.path.parent / temporary, rename=True)
                try:
                    info = fs._file_info(handle)
                    if info.st_nlink != 1 or info.st_file_attributes & fs._REPARSE:
                        raise _error("AUTH_CACHE_UNSAFE", "The credential staging file changed.")
                    fs._rename_same_directory(handle, self.path.name)
                finally:
                    fs._kernel.CloseHandle(handle)
            else:
                os.replace(temporary, self.path.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                os.fsync(self.fd)
            published = True
            self.check()
        finally:
            if not published:
                with contextlib.suppress(OSError):
                    if os.name == "nt":
                        (self.path.parent / temporary).unlink()
                    else:
                        os.unlink(temporary, dir_fd=self.fd)

    @contextlib.contextmanager
    def publication_lock(self, timeout):
        """Serialize account-bound publication across a broker and login CLI."""
        self.check()
        fd = None
        try:
            if os.name == "nt":
                from . import fs_access as fs
                handle = fs._handle(self.path.parent / ".auth.lock", write=True, create=True)
                try:
                    info = fs._file_info(handle)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_file_attributes & fs._REPARSE or
                            fs._final_path(handle) != fs._canonical(self.path.parent / ".auth.lock")):
                        raise _error("AUTH_CACHE_UNSAFE", "The credential lock is redirected.")
                    fd = fs.msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
                    handle = None
                finally:
                    if handle is not None:
                        fs._kernel.CloseHandle(handle)
                # _locking can lock beyond EOF, so an empty lock file needs no
                # initialization write. Two first publishers may both observe
                # size zero: writing here could hit a byte range already locked
                # by the other handle and fail with a Windows sharing violation.
            else:
                # macOS may return transient ENOENT when concurrent O_CREAT |
                # O_NOFOLLOW calls first create this entry. Keep the directory
                # pinned and retry only that error; links and permissions still
                # fail immediately, and a changed directory is never retried.
                for attempt in range(3):
                    try:
                        fd = os.open(".auth.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
                        break
                    except FileNotFoundError:
                        self.check()
                        if attempt == 2:
                            raise
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or
                        stat.S_IMODE(info.st_mode) & 0o077):
                    raise _error("AUTH_CACHE_UNSAFE", "The credential lock is not a private ordinary file.")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise _error("AUTH_CACHE_BUSY", "Another process is updating this profile's authorization.", True)
                    time.sleep(0.05)
            self.check()
            yield
        except DriveError:
            raise
        except OSError:
            raise _error("AUTH_CACHE_UNSAFE", "The credential update could not be locked safely.") from None
        finally:
            if fd is not None:
                os.close(fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.guard is not None:
            self.guard.close()
            self.guard = None


def _read_cache(path):
    try:
        directory = _CacheDirectory(path)
    except FileNotFoundError:
        return None
    try:
        return directory.read()
    except DriveError:
        raise
    except OSError:
        raise _error("AUTH_CACHE_UNSAFE", "The credential cache cannot be read safely.") from None
    finally:
        directory.close()


def _write_cache(path, value):
    directory = _CacheDirectory(path, create=True)
    try:
        directory.write(value)
    except DriveError:
        raise
    except OSError:
        raise _error("AUTH_CACHE_UNSAFE", "The credential cache cannot be written safely.") from None
    finally:
        directory.close()


def _publish_bound_cache(path, value, config):
    directory = _CacheDirectory(path, create=True)
    try:
        with directory.publication_lock(config["timeout_seconds"]):
            previous = _validate_cache(directory.read(), config)
            if previous and previous["account_binding"] != value["account_binding"]:
                raise _error("AUTH_ACCOUNT_MISMATCH", "This profile is already bound to a different account; use a separate profile.")
            directory.write(value)
    except DriveError:
        raise
    except OSError:
        raise _error("AUTH_CACHE_UNSAFE", "The credential cache cannot be written safely.") from None
    finally:
        directory.close()


def _account_binding(tenant, account, client):
    return _hash(_json([tenant, account, client]))


def _validate_cache(cache, config):
    if cache is None:
        return None
    try:
        if (cache.get("version") != 1 or cache.get("authority") != _AUTHORITY or
                cache.get("tenant_selector") != config["tenant_id"] or cache.get("client_id") != config["client_id"] or
                cache.get("scopes") != config["scopes"] or not _credential(cache.get("refresh_token"))):
            raise ValueError()
        tenant, account = _uuid(cache["tenant_id"]), _uuid(cache["object_id"])
        if cache.get("account_binding") != _account_binding(tenant, account, config["client_id"]):
            raise ValueError()
        _check_tenant(config["tenant_id"], tenant)
        return cache
    except (KeyError, TypeError, ValueError):
        raise _error("AUTH_BINDING_MISMATCH", "The saved credential belongs to a different client, tenant, account, or scope configuration.") from None


def _check_tenant(selector, tenant):
    if ((selector == "consumers" and tenant != _CONSUMER_TENANT) or
            (selector == "organizations" and tenant == _CONSUMER_TENANT) or
            (selector not in ("common", "organizations", "consumers") and selector != tenant)):
        raise _error("AUTH_BINDING_MISMATCH", "The authenticated tenant does not match the configured tenant.")


def _identity_from_response(response, config):
    # Decode ONLY the ID token supplied by our direct HTTPS token response.
    # Never inspect Graph access tokens, env tokens, or externally supplied JWTs.
    try:
        token = response.get("id_token")
        if not isinstance(token, str) or len(token) > 131072 or len(token.split(".")) != 3:
            raise ValueError()
        part = token.split(".")[1]
        claims = bounded_json_loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        tenant, account = _uuid(claims["tid"]), _uuid(claims["oid"])
        if claims.get("aud") != config["client_id"] or claims.get("iss") != _AUTHORITY + "/" + tenant + "/v2.0":
            raise ValueError()
        expiry = claims.get("exp")
        not_before = claims.get("nbf", 0)
        if (isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= time.time() - 60 or
                isinstance(not_before, bool) or not isinstance(not_before, (int, float)) or not math.isfinite(not_before) or not_before > time.time() + 60):
            raise ValueError()
        _check_tenant(config["tenant_id"], tenant)
        return tenant, account
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise _error("AUTH_BINDING_MISMATCH", "Microsoft's identity response could not be bound to this client and account.") from None


def _accept_response(response, config, previous=None):
    try:
        access, refresh = response.get("access_token"), response.get("refresh_token")
        ttl = response.get("expires_in")
        if (not _credential(access) or not _credential(refresh) or response.get("token_type", "").casefold() != "bearer" or
                isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= 86400):
            raise ValueError()
        granted = {_response_scope(value) for value in response["scope"].split()}
        if not set(config["scopes"]).issubset(granted) or granted - set(config["scopes"]) - _IDENTITY_SCOPES - {"User.Read"}:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _error("AUTH_INVALID_RESPONSE", "The authorization response lacks the requested read-only credential fields.") from None
    tenant, account = _identity_from_response(response, config)
    binding = _account_binding(tenant, account, config["client_id"])
    if previous is not None and previous["account_binding"] != binding:
        raise _error("AUTH_ACCOUNT_MISMATCH", "This profile is already bound to a different account; use a separate profile.")
    cache = {"version": 1, "authority": _AUTHORITY, "tenant_selector": config["tenant_id"],
             "tenant_id": tenant, "client_id": config["client_id"], "object_id": account,
             "account_binding": binding, "scopes": config["scopes"],
             "refresh_token": refresh, "saved_at": time.time()}
    return access, time.time() + ttl - min(60, ttl / 10), cache


class TokenProvider:
    def __init__(self, config, token_cache_path):
        self.config = validate_auth(config)
        self.cache_path = Path(token_cache_path) if token_cache_path is not None else None
        if self.cache_path is None and not self.config.get("token_env"):
            raise _error("INVALID_CONFIG", "A private Graph credential-cache path is required.")
        self._access = None
        self._expiry = 0
        self._principal = None
        self._env_fingerprint = None
        self._lock = threading.RLock()

    def _environment_token(self):
        token = os.environ.get(self.config["token_env"])
        if not _credential(token):
            raise _error("AUTH_REQUIRED", "The configured access-token environment variable is missing or invalid.")
        fingerprint = _hash(token)
        with self._lock:
            if self._env_fingerprint is not None and self._env_fingerprint != fingerprint:
                raise _error("AUTH_ACCOUNT_MISMATCH", "The external credential changed while this profile was running; initialize a separate profile and state.")
            self._env_fingerprint = fingerprint
        return token, fingerprint

    @property
    def account_binding(self):
        if self.config.get("token_env"):
            _, fingerprint = self._environment_token()
            return "env:" + _hash(_json([self.config["tenant_id"], self.config["client_id"], self.config["token_env"], fingerprint]))
        cache = _validate_cache(_read_cache(self.cache_path), self.config)
        if cache is None:
            raise _error("AUTH_REQUIRED", "No authorization has been saved for this profile.")
        self._bind_principal(cache["account_binding"])
        return cache["account_binding"]

    def _bind_principal(self, binding):
        with self._lock:
            if self._principal is not None and self._principal != binding:
                self._access, self._expiry = None, 0
                raise _error("AUTH_ACCOUNT_MISMATCH", "The credential account changed while this profile was running; use a separate profile.")
            self._principal = binding

    def token(self):
        """Return a credential without ever initiating interactive authorization."""
        if self.config.get("token_env"):
            return self._environment_token()[0]
        with self._lock:
            cache = _validate_cache(_read_cache(self.cache_path), self.config)
            if cache is None:
                raise _error("AUTH_REQUIRED", "No authorization has been saved for this profile.")
            self._bind_principal(cache["account_binding"])
            if self._access is not None and time.time() < self._expiry:
                return self._access
            try:
                response = _request(cache["tenant_id"], "token", {
                    "client_id": self.config["client_id"], "grant_type": "refresh_token",
                    "refresh_token": cache["refresh_token"],
                    "scope": " ".join(self.config["scopes"] + sorted(_IDENTITY_SCOPES))}, self.config["timeout_seconds"])
            except _OAuthError as exc:
                self._access, self._expiry = None, 0
                raise _oauth_failure(exc) from None
            access, expiry, updated = _accept_response(response, self.config, cache)
            _publish_bound_cache(self.cache_path, updated, self.config)
            self._access, self._expiry = access, expiry
            self._principal = cache["account_binding"]
            return access


def _cancelled(cancellation):
    return bool(cancellation and (cancellation() if callable(cancellation) else cancellation.is_set()))


def _wait(interval, deadline, cancellation):
    until = min(deadline, time.monotonic() + interval)
    while time.monotonic() < until:
        if _cancelled(cancellation):
            raise _error("AUTH_CANCELLED", "Authorization was cancelled.")
        time.sleep(max(0, min(0.25, until - time.monotonic())))
    if _cancelled(cancellation):
        raise _error("AUTH_CANCELLED", "Authorization was cancelled.")
    if time.monotonic() >= deadline:
        raise _error("AUTH_EXPIRED", "The device authorization request expired.")


def device_login(config, cache_path, emit, cancellation=None):
    """Interactive device flow. Invoke only after an explicit user's login action."""
    config = validate_auth(config)
    if config.get("token_env"):
        raise _error("INVALID_CONFIG", "This profile uses an externally supplied token; remove token_env before device login.")
    if _cancelled(cancellation):
        raise _error("AUTH_CANCELLED", "Authorization was cancelled.")
    # Validate private storage before asking the user to sign in.
    directory = _CacheDirectory(cache_path, create=True)
    try:
        previous = _validate_cache(directory.read(), config)
    finally:
        directory.close()
    try:
        started = time.monotonic()
        code = _request(config["tenant_id"], "devicecode", {
            "client_id": config["client_id"], "scope": " ".join(config["scopes"] + sorted(_IDENTITY_SCOPES))},
            config["timeout_seconds"])
        try:
            if not isinstance(code.get("verification_uri"), str):
                raise ValueError()
            uri = urllib.parse.urlsplit(code["verification_uri"])
            port = uri.port
        except (ValueError, TypeError):
            raise _error("AUTH_INVALID_RESPONSE", "The device authorization instructions were invalid.") from None
        user_code = code.get("user_code")
        expires, interval = code.get("expires_in"), code.get("interval", 5)
        if (not _credential(code.get("device_code")) or not isinstance(user_code, str) or
                not re.fullmatch(r"[A-Za-z0-9-]{4,32}", user_code) or uri.scheme != "https" or
                uri.hostname not in {"microsoft.com", "www.microsoft.com", "login.microsoftonline.com"} or
                uri.username is not None or uri.password is not None or uri.query or uri.fragment or port not in (None, 443) or
                isinstance(expires, bool) or not isinstance(expires, int) or not 1 <= expires <= 1800 or
                isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 60):
            raise _error("AUTH_INVALID_RESPONSE", "The device authorization instructions were invalid.")
        emit({"stage": "device_code", "verification_uri": code["verification_uri"], "user_code": user_code,
              "expires_in": expires, "interval": interval})
        deadline = started + expires
        while True:
            _wait(interval, deadline, cancellation)
            try:
                response = _request(config["tenant_id"], "token", {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": config["client_id"], "device_code": code["device_code"]},
                    min(config["timeout_seconds"], max(0.1, deadline - time.monotonic())))
            except _OAuthError as exc:
                if exc.code in {"authorization_pending", "slow_down"}:
                    if exc.code == "slow_down":
                        interval = min(60, interval + 5)
                    continue
                raise
            access, expiry, cache = _accept_response(response, config, previous)
            _publish_bound_cache(cache_path, cache, config)
            return {"authenticated": True, "tenant_id": cache["tenant_id"], "client_id": cache["client_id"],
                    "account_id": cache["account_binding"], "scopes": config["scopes"], "cache_saved": True}
    except _OAuthError as exc:
        raise _oauth_failure(exc) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit Microsoft device login for a configured cloud profile.")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config_path = Path(args.config).expanduser().resolve(strict=True)
        with config_path.open("rb") as source:
            raw = source.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError()
        config = bounded_json_loads(raw)
        graph = config["graph"]
        cache = graph.get("auth_cache")
        if not isinstance(cache, str) or not cache:
            raise ValueError()
        cache = Path(cache).expanduser()
        if not cache.is_absolute():
            cache = config_path.parent / cache
        result = device_login(graph, cache, lambda value: print(_json(value), flush=True))
        print(_json(dict(stage="complete", **result)), flush=True)
        return 0
    except KeyboardInterrupt:
        print(_json({"ok": False, "error": {"code": "AUTH_CANCELLED", "message": "Authorization was cancelled."}}), flush=True)
        return 130
    except DriveError as exc:
        print(_json({"ok": False, "error": {"code": exc.code, "message": exc.message,
            "retryable": exc.retryable, "next_action": exc.next_action}}), flush=True)
        return 130 if exc.code == "AUTH_CANCELLED" else 1
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        print(_json({"ok": False, "error": {"code": "INVALID_CONFIG", "message": "Use a valid cloud profile with a private auth_cache path."}}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
