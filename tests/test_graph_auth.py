"""Synthetic Microsoft OAuth responses; these tests never contact Microsoft."""
import base64
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.parse
from unittest import mock

from team_drive.errors import DriveError
from team_drive import graph_auth as auth
from team_drive import bounded_json


TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ACCOUNT = "33333333-3333-3333-3333-333333333333"
OTHER_ACCOUNT = "44444444-4444-4444-4444-444444444444"
CONFIG = {"tenant_id": TENANT, "client_id": CLIENT}


def identity(tenant=TENANT, account=ACCOUNT, client=CLIENT, **changes):
    claims = {"tid": tenant, "oid": account, "aud": client,
              "iss": auth._AUTHORITY + "/" + tenant + "/v2.0", "exp": time.time() + 3600, "nbf": time.time() - 5}
    claims.update(changes)
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "RS256", "typ": "JWT"}) + "." + encode(claims) + ".synthetic-signature"


def token_response(refresh="SYNTHETIC_REFRESH_SECRET", access="SYNTHETIC_ACCESS_SECRET", **claims):
    return {"token_type": "Bearer", "scope": "Files.Read openid profile", "expires_in": 3600,
            "access_token": access, "refresh_token": refresh, "id_token": identity(**claims)}


def device_response():
    return {"device_code": "SYNTHETIC_DEVICE_SECRET", "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900, "interval": 5,
            "message": "DO_NOT_PRINT_REMOTE_MESSAGE"}


class FakeResponse:
    def __init__(self, value, status=200):
        self.status = status
        self.body = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())

    def read1(self, size):
        return self.body.read(size)

    def close(self):
        self.body.close()


class BoundedJSONTests(unittest.TestCase):
    def test_container_depth_has_a_fixed_boundary(self):
        for opening, closing in (("[", "]"), ('{"nested":', "}")):
            with self.subTest(opening=opening):
                bounded_json.loads(opening * 64 + "0" + closing * 64)
                with self.assertRaises(ValueError):
                    bounded_json.loads(opening * 65 + "0" + closing * 65)
        self.assertEqual(bounded_json.loads('{"items":[{},[]]}', max_depth=3), {"items": [{}, []]})
        with self.assertRaises(ValueError):
            bounded_json.loads('{"items":[{},[]]}', max_depth=2)

    def test_scalars_empty_containers_and_invalid_limits(self):
        self.assertEqual(bounded_json.loads("42", max_depth=0), 42)
        self.assertIsNone(bounded_json.loads("null", max_depth=0))
        for value in ("[]", "{}"):
            with self.assertRaises(ValueError):
                bounded_json.loads(value, max_depth=0)
            bounded_json.loads(value, max_depth=1)
        for limit in (-1, True, 1.5, "64", None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                bounded_json.loads("{}", max_depth=limit)

    def test_decoder_recursion_error_is_normalized(self):
        with mock.patch.object(bounded_json.json, "loads", side_effect=RecursionError("version-dependent decoder limit")), \
             self.assertRaises(ValueError) as error:
            bounded_json.loads("[]")
        self.assertNotIn("version-dependent", str(error.exception))


class GraphAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="df-graph-auth-")
        self.base = Path(self.temporary.name).resolve()
        self.cache = self.base / "profile" / "auth.json"
        self.no_network = mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Real networking is forbidden in auth tests"))
        self.no_network.start()

    def tearDown(self):
        self.no_network.stop()
        self.temporary.cleanup()

    def login(self, config=None, response=None):
        events = []
        with mock.patch.object(auth, "_request", side_effect=[device_response(), response or token_response()]) as request, \
             mock.patch.object(auth, "_wait"):
            result = auth.device_login(config or CONFIG, self.cache, events.append)
        return result, events, request

    def test_validate_normalizes_read_scopes_and_has_no_token_value(self):
        self.assertEqual(auth.validate_auth(CONFIG), dict(CONFIG, scopes=["Files.Read"], timeout_seconds=10.0))
        config = dict(CONFIG, scopes=["https://graph.microsoft.com/files.read.all", "Sites.Read.All", "openid", "offline_access"],
                      token_env="DF_TEST_GRAPH_TOKEN", timeout_seconds=0.1)
        normalized = auth.validate_auth(config)
        self.assertEqual(normalized["scopes"], ["Files.Read.All", "Sites.Read.All"])
        self.assertEqual(normalized["token_env"], "DF_TEST_GRAPH_TOKEN")
        for tenant in ("common", "organizations", "consumers", TENANT.upper()):
            self.assertTrue(auth.validate_auth(dict(CONFIG, tenant_id=tenant)))

    def test_invalid_authority_write_scopes_env_values_and_bounds_rejected(self):
        cases = [{"tenant_id": "https://attacker.invalid"}, {"tenant_id": "../common"}, {"client_id": "not-a-uuid"},
                 {"scopes": ["Files.ReadWrite"]}, {"scopes": ["Files.ReadWrite.All"]}, {"scopes": ["https://graph.microsoft.com/.default"]},
                 {"scopes": ["Mail.Read"]}, {"scopes": ["openid"]}, {"scopes": []}, {"scopes": "Files.Read"},
                 {"token_env": None}, {"token_env": "Bearer SECRET"}, {"token_env": ""},
                 {"timeout_seconds": 0.09}, {"timeout_seconds": 21}, {"timeout_seconds": True}]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(DriveError) as error:
                auth.validate_auth(dict(CONFIG, **change))
            self.assertEqual(error.exception.code, "INVALID_CONFIG")

    def test_missing_cache_never_starts_login_or_creates_directory(self):
        provider = auth.TokenProvider(CONFIG, self.cache)
        with mock.patch.object(auth, "device_login", side_effect=AssertionError("No implicit login")), \
             mock.patch.object(auth, "_request", side_effect=AssertionError("No implicit network")):
            for operation in (provider.token, lambda: provider.account_binding):
                with self.assertRaises(DriveError) as error:
                    operation()
                self.assertEqual(error.exception.code, "AUTH_REQUIRED")
        self.assertFalse(self.cache.parent.exists())

    def test_env_token_is_opaque_not_cached_and_binding_is_not_an_account_claim(self):
        config = dict(CONFIG, token_env="DF_TEST_GRAPH_TOKEN")
        provider = auth.TokenProvider(config, self.cache)
        with mock.patch.dict(os.environ, {"DF_TEST_GRAPH_TOKEN": "opaque-synthetic-access"}):
            self.assertEqual(provider.token(), "opaque-synthetic-access")
            before = provider.account_binding
        with mock.patch.dict(os.environ, {"DF_TEST_GRAPH_TOKEN": "different-opaque-token"}):
            with self.assertRaises(DriveError) as error:
                provider.token()
            self.assertEqual(error.exception.code, "AUTH_ACCOUNT_MISMATCH")
            replacement = auth.TokenProvider(config, self.cache)
            self.assertNotEqual(replacement.account_binding, before)
            self.assertEqual(replacement.token(), "different-opaque-token")
        self.assertTrue(before.startswith("env:"))
        self.assertFalse(self.cache.parent.exists())
        with mock.patch.dict(os.environ, {"DF_TEST_GRAPH_TOKEN": "bad\nheader"}), self.assertRaises(DriveError) as error:
            provider.token()
        self.assertEqual(error.exception.code, "AUTH_REQUIRED")
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(DriveError) as error:
            auth.TokenProvider(config, self.cache).account_binding
        self.assertEqual(error.exception.code, "AUTH_REQUIRED")

    def test_explicit_login_requests_only_read_and_identity_scopes_and_emits_no_tokens(self):
        result, events, request = self.login()
        self.assertTrue(result["authenticated"])
        self.assertEqual(events[0]["stage"], "device_code")
        self.assertEqual(events[0]["user_code"], "ABCD-EFGH")
        output = json.dumps([events, result])
        for forbidden in ("SYNTHETIC_DEVICE_SECRET", "SYNTHETIC_ACCESS_SECRET", "SYNTHETIC_REFRESH_SECRET", "DO_NOT_PRINT_REMOTE_MESSAGE"):
            self.assertNotIn(forbidden, output)
        fields = request.call_args_list[0].args[2]
        self.assertEqual(set(fields["scope"].split()), {"Files.Read", "offline_access", "openid", "profile"})
        stored = auth._read_cache(self.cache)
        self.assertIn("refresh_token", stored)
        self.assertNotIn("access_token", stored)
        self.assertNotIn("id_token", stored)
        self.assertEqual(auth.TokenProvider(CONFIG, self.cache).account_binding, result["account_id"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.cache.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        else:
            self.assertTrue(self.cache.read_bytes().startswith(b"DFG1\0"))
            self.assertNotIn(b"SYNTHETIC_REFRESH_SECRET", self.cache.read_bytes())

    def test_pending_and_slow_down_respect_polling_intervals(self):
        with mock.patch.object(auth, "_request", side_effect=[device_response(), auth._OAuthError("authorization_pending"),
             auth._OAuthError("slow_down"), token_response()]), mock.patch.object(auth, "_wait") as wait:
            auth.device_login(CONFIG, self.cache, lambda event: None)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [5, 5, 10])

    def test_cancellation_before_login_makes_no_requests(self):
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(auth, "_request") as request, self.assertRaises(DriveError) as error:
            auth.device_login(CONFIG, self.cache, lambda event: None, cancellation=cancelled)
        self.assertEqual(error.exception.code, "AUTH_CANCELLED")
        request.assert_not_called()
        self.assertFalse(self.cache.parent.exists())

    def test_decline_expiry_and_invalid_grant_errors_are_sanitized(self):
        for remote, public in (("authorization_declined", "AUTH_DECLINED"), ("expired_token", "AUTH_EXPIRED"),
                               ("invalid_grant", "AUTH_REQUIRED"), ("unknown", "AUTH_FAILED")):
            with self.subTest(remote=remote), mock.patch.object(auth, "_request", side_effect=[device_response(), auth._OAuthError(remote)]), \
                 mock.patch.object(auth, "_wait"), self.assertRaises(DriveError) as error:
                auth.device_login(CONFIG, self.cache, lambda event: None)
            self.assertEqual(error.exception.code, public)
            self.assertNotIn("SECRET", str(error.exception))
        with self.assertRaises(DriveError) as error:
            auth._wait(1, time.monotonic() - 1, None)
        self.assertEqual(error.exception.code, "AUTH_EXPIRED")

    def test_refresh_rotates_private_credential_and_memory_access_is_reused(self):
        self.login()
        provider = auth.TokenProvider(CONFIG, self.cache)
        with mock.patch.object(auth, "_request", return_value=token_response(refresh="ROTATED_REFRESH", access="ROTATED_ACCESS")) as request:
            self.assertEqual(provider.token(), "ROTATED_ACCESS")
            self.assertEqual(provider.token(), "ROTATED_ACCESS")
        request.assert_called_once()
        fields = request.call_args.args[2]
        self.assertEqual(fields["grant_type"], "refresh_token")
        self.assertEqual(fields["refresh_token"], "SYNTHETIC_REFRESH_SECRET")
        self.assertEqual(auth._read_cache(self.cache)["refresh_token"], "ROTATED_REFRESH")
        self.assertNotIn(b"ROTATED_ACCESS", self.cache.read_bytes())

    def test_revoked_refresh_requires_explicit_relogin_and_does_not_open_device_flow(self):
        self.login()
        provider = auth.TokenProvider(CONFIG, self.cache)
        with mock.patch.object(auth, "_request", side_effect=auth._OAuthError("invalid_grant")) as request, \
             mock.patch.object(auth, "device_login", side_effect=AssertionError("Must not prompt")), self.assertRaises(DriveError) as error:
            provider.token()
        self.assertEqual(error.exception.code, "AUTH_REQUIRED")
        self.assertEqual(request.call_args.args[1], "token")

    def test_refresh_and_explicit_relogin_reject_different_account_without_overwriting_cache(self):
        self.login()
        original = self.cache.read_bytes()
        with mock.patch.object(auth, "_request", return_value=token_response(account=OTHER_ACCOUNT)), self.assertRaises(DriveError) as error:
            auth.TokenProvider(CONFIG, self.cache).token()
        self.assertEqual(error.exception.code, "AUTH_ACCOUNT_MISMATCH")
        with mock.patch.object(auth, "_request", side_effect=[device_response(), token_response(account=OTHER_ACCOUNT)]), \
             mock.patch.object(auth, "_wait"), self.assertRaises(DriveError) as error:
            auth.device_login(CONFIG, self.cache, lambda event: None)
        self.assertEqual(error.exception.code, "AUTH_ACCOUNT_MISMATCH")
        self.assertEqual(self.cache.read_bytes(), original)

    def test_concurrent_first_logins_cannot_publish_two_different_accounts(self):
        barrier = threading.Barrier(2)
        outcomes = []
        diagnostics = []

        def request(tenant, action, fields, timeout):
            if action == "devicecode":
                return device_response()
            barrier.wait(timeout=3)
            return token_response(account=ACCOUNT if threading.current_thread().name == "first" else OTHER_ACCOUNT)

        def login():
            try:
                result = auth.device_login(CONFIG, self.cache, lambda event: None)
                outcomes.append(("ok", result["account_id"]))
            except DriveError as exc:
                outcomes.append((exc.code, None))
                diagnostics.append((exc.code, exc.message))

        with mock.patch.object(auth, "_request", side_effect=request), mock.patch.object(auth, "_wait"):
            threads = [threading.Thread(target=login, name=name) for name in ("first", "second")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(item[0] for item in outcomes), ["AUTH_ACCOUNT_MISMATCH", "ok"], diagnostics)
        expected = next(binding for state, binding in outcomes if state == "ok")
        self.assertEqual(auth.TokenProvider(CONFIG, self.cache).account_binding, expected)

    def test_windows_stale_empty_lock_metadata_does_not_write_into_competing_lock(self):
        # Reproduce the Windows first-create ordering on every CI platform:
        # this handle observed size=0, then another handle took byte 0. Windows
        # rejects an initialization write, whereas lock contention must retry.
        from pathlib import PureWindowsPath
        from team_drive import fs_access as fs
        directory = object.__new__(auth._CacheDirectory)
        directory.path = PureWindowsPath(r"C:\synthetic-profile\auth.json")
        directory.check = mock.Mock()
        locking = mock.Mock(side_effect=[PermissionError(errno.EACCES, "synthetic competing lock"), None])
        runtime = SimpleNamespace(open_osfhandle=mock.Mock(return_value=73), locking=locking, LK_NBLCK=2)
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_file_attributes=0, st_size=0)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(auth.os, "name", "nt"))
            stack.enter_context(mock.patch.object(auth.os, "O_BINARY", 0, create=True))
            stack.enter_context(mock.patch.object(auth.os, "lseek"))
            closed = stack.enter_context(mock.patch.object(auth.os, "close"))
            wrote = stack.enter_context(mock.patch.object(auth.os, "write", side_effect=PermissionError(errno.EACCES, "synthetic locked-byte write")))
            stack.enter_context(mock.patch.object(auth.time, "sleep"))
            stack.enter_context(mock.patch.object(fs, "_handle", return_value=91, create=True))
            stack.enter_context(mock.patch.object(fs, "_file_info", return_value=info, create=True))
            stack.enter_context(mock.patch.object(fs, "_REPARSE", 0x400, create=True))
            stack.enter_context(mock.patch.object(fs, "_final_path", return_value="same-pinned-path", create=True))
            stack.enter_context(mock.patch.object(fs, "_canonical", return_value="same-pinned-path", create=True))
            stack.enter_context(mock.patch.object(fs, "msvcrt", runtime, create=True))
            stack.enter_context(mock.patch.dict(sys.modules, {"msvcrt": runtime}))
            with directory.publication_lock(1):
                self.assertEqual(locking.call_count, 2)
            wrote.assert_not_called()
            closed.assert_called_once_with(73)
        directory.check.assert_called()

    @unittest.skipUnless(os.name == "nt", "Windows native byte-range lock contract")
    def test_windows_empty_lock_excludes_another_handle_and_releases_on_close(self):
        first = auth._CacheDirectory(self.cache, create=True)
        second = auth._CacheDirectory(self.cache, create=True)
        try:
            with first.publication_lock(1):
                self.assertEqual((self.cache.parent / ".auth.lock").stat().st_size, 0)
                with self.assertRaises(DriveError) as error:
                    with second.publication_lock(0.01):
                        self.fail("A second handle cannot enter the locked publication region")
                self.assertEqual(error.exception.code, "AUTH_CACHE_BUSY")
            with second.publication_lock(1):
                pass
        finally:
            first.close()
            second.close()

    @unittest.skipIf(os.name == "nt", "POSIX O_CREAT/O_NOFOLLOW race; Windows uses guarded handles")
    def test_publication_lock_retries_only_transient_missing_entry(self):
        directory = auth._CacheDirectory(self.cache, create=True)
        original_open = os.open
        try:
            attempts = []

            def transient_open(name, *args, **kwargs):
                if name == ".auth.lock":
                    attempts.append(name)
                    if len(attempts) <= 2:
                        raise FileNotFoundError(errno.ENOENT, "Synthetic first-create race")
                return original_open(name, *args, **kwargs)

            with mock.patch.object(auth.os, "open", side_effect=transient_open), \
                 mock.patch.object(directory, "check", wraps=directory.check) as check:
                with directory.publication_lock(1):
                    self.assertEqual(len(attempts), 3)
                self.assertGreaterEqual(check.call_count, 4)

            for failure, expected_attempts in ((FileNotFoundError(errno.ENOENT, "still missing"), 3),
                                               (OSError(errno.ELOOP, "symlink"), 1),
                                               (PermissionError(errno.EACCES, "denied"), 1)):
                with self.subTest(failure=failure), mock.patch.object(auth.os, "open", side_effect=failure) as opened, \
                     self.assertRaises(DriveError) as error:
                    with directory.publication_lock(1):
                        self.fail("Unsafe or inaccessible lock must never be acquired")
                self.assertEqual(error.exception.code, "AUTH_CACHE_UNSAFE")
                self.assertEqual(opened.call_count, expected_attempts)
        finally:
            directory.close()

    @unittest.skipIf(os.name == "nt", "POSIX O_CREAT/O_NOFOLLOW race; Windows uses guarded handles")
    def test_publication_lock_does_not_retry_after_directory_replacement(self):
        directory = auth._CacheDirectory(self.cache, create=True)
        displaced = self.cache.parent.with_name("displaced")
        try:
            def replace_directory(*args, **kwargs):
                self.cache.parent.rename(displaced)
                self.cache.parent.mkdir(mode=0o700)
                raise FileNotFoundError(errno.ENOENT, "Synthetic replaced-directory race")

            with mock.patch.object(auth.os, "open", side_effect=replace_directory) as opened, self.assertRaises(DriveError) as error:
                with directory.publication_lock(1):
                    self.fail("A changed credential directory must never be accepted")
            self.assertEqual(error.exception.code, "AUTH_CACHE_UNSAFE")
            opened.assert_called_once()
        finally:
            directory.close()

    def test_running_provider_rejects_disk_account_switch_before_using_memory_token(self):
        self.login()
        provider = auth.TokenProvider(CONFIG, self.cache)
        with mock.patch.object(auth, "_request", return_value=token_response()):
            provider.token()
        alternate = auth._accept_response(token_response(account=OTHER_ACCOUNT), auth.validate_auth(CONFIG))[2]
        auth._write_cache(self.cache, alternate)
        with self.assertRaises(DriveError) as error:
            provider.token()
        self.assertEqual(error.exception.code, "AUTH_ACCOUNT_MISMATCH")

    def test_account_binding_pins_principal_before_first_token_request(self):
        self.login()
        provider = auth.TokenProvider(CONFIG, self.cache)
        original_binding = provider.account_binding
        alternate = auth._accept_response(token_response(account=OTHER_ACCOUNT), auth.validate_auth(CONFIG))[2]
        self.assertNotEqual(alternate["account_binding"], original_binding)
        auth._write_cache(self.cache, alternate)
        with mock.patch.object(auth, "_request") as request, self.assertRaises(DriveError) as error:
            provider.token()
        self.assertEqual(error.exception.code, "AUTH_ACCOUNT_MISMATCH")
        request.assert_not_called()

    def test_cache_client_tenant_and_scope_mismatches_fail_before_network(self):
        self.login()
        for config in (dict(CONFIG, client_id=OTHER_ACCOUNT), dict(CONFIG, tenant_id="organizations"),
                       dict(CONFIG, scopes=["Files.Read.All"])):
            with self.subTest(config=config), mock.patch.object(auth, "_request") as request, self.assertRaises(DriveError) as error:
                auth.TokenProvider(config, self.cache).token()
            self.assertEqual(error.exception.code, "AUTH_BINDING_MISMATCH")
            request.assert_not_called()

    def test_identity_claims_are_bound_to_client_tenant_issuer_and_lifetime(self):
        for claims in ({"client": OTHER_ACCOUNT}, {"tenant": OTHER_ACCOUNT}, {"iss": "https://attacker.invalid"},
                       {"exp": time.time() - 3600}, {"nbf": time.time() + 3600}):
            with self.subTest(claims=claims), self.assertRaises(DriveError) as error:
                self.login(response=token_response(**claims))
            self.assertEqual(error.exception.code, "AUTH_BINDING_MISMATCH")
        self.assertFalse(self.cache.exists())

    def test_broader_write_or_unrequested_read_token_scopes_are_rejected(self):
        for scope in ("Files.Read Files.ReadWrite", "Files.Read Sites.Read.All", "openid profile"):
            response = dict(token_response(), scope=scope)
            with self.subTest(scope=scope), self.assertRaises(DriveError) as error:
                self.login(response=response)
            self.assertEqual(error.exception.code, "AUTH_INVALID_RESPONSE")

    def test_previously_granted_user_read_is_tolerated_only_in_response(self):
        response = dict(token_response(), scope="Files.Read openid profile https://graph.microsoft.com/User.Read")
        result, events, request = self.login(response=response)
        self.assertTrue(result["authenticated"])
        self.assertNotIn("User.Read", request.call_args_list[0].args[2]["scope"].split())
        self.assertEqual(result["scopes"], ["Files.Read"])
        with self.assertRaises(DriveError) as error:
            auth.validate_auth(dict(CONFIG, scopes=["Files.Read", "User.Read"]))
        self.assertEqual(error.exception.code, "INVALID_CONFIG")

    def test_refresh_without_identity_token_fails_without_replacing_cache(self):
        self.login()
        original = self.cache.read_bytes()
        response = token_response(refresh="NEW_REFRESH_NOT_SAVED")
        del response["id_token"]
        with mock.patch.object(auth, "_request", return_value=response), self.assertRaises(DriveError) as error:
            auth.TokenProvider(CONFIG, self.cache).token()
        self.assertEqual(error.exception.code, "AUTH_BINDING_MISMATCH")
        self.assertEqual(self.cache.read_bytes(), original)

    def test_deep_json_cache_identity_transport_and_config_fail_without_traceback(self):
        deeply_nested = '{"x":' + '[' * 2000 + '0' + ']' * 2000 + '}'
        self.cache.parent.mkdir(mode=0o700)
        contents = deeply_nested.encode()
        if os.name == "nt":
            contents = b"DFG1\0" + auth._dpapi(contents)
        self.cache.write_bytes(contents)
        self.cache.chmod(0o600)
        with self.assertRaises(DriveError) as error:
            auth._read_cache(self.cache)
        self.assertEqual(error.exception.code, "AUTH_CACHE_INVALID")
        encoded = base64.urlsafe_b64encode(deeply_nested.encode()).decode().rstrip("=")
        with self.assertRaises(DriveError) as error:
            auth._identity_from_response({"id_token": "header." + encoded + ".signature"}, auth.validate_auth(CONFIG))
        self.assertEqual(error.exception.code, "AUTH_BINDING_MISMATCH")
        opener = mock.Mock()
        opener.open.return_value = FakeResponse(deeply_nested.encode())
        with mock.patch("urllib.request.build_opener", return_value=opener), self.assertRaises(DriveError) as error:
            auth._request(TENANT, "token", {}, 1)
        self.assertEqual(error.exception.code, "AUTH_INVALID_RESPONSE")
        config = self.base / "deep-config.json"
        config.write_text(deeply_nested, encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = auth.main(["--config", str(config)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "INVALID_CONFIG")
        self.assertNotIn("Traceback", output.getvalue())

    def test_invalid_device_verification_uri_and_fields_are_not_emitted(self):
        for change in ({"verification_uri": "https://attacker.invalid/login"}, {"verification_uri": 123},
                       {"verification_uri": "https://microsoft.com:invalid/login"}, {"user_code": "abc\nSECRET"},
                       {"interval": 0}, {"expires_in": 999999}):
            emitted = []
            with self.subTest(change=change), mock.patch.object(auth, "_request", return_value=dict(device_response(), **change)), self.assertRaises(DriveError) as error:
                auth.device_login(CONFIG, self.cache, emitted.append)
            self.assertEqual(error.exception.code, "AUTH_INVALID_RESPONSE")
            self.assertEqual(emitted, [])

    @unittest.skipIf(os.name == "nt", "POSIX permission/symlink checks; Windows cache uses DPAPI and guarded handles")
    def test_cache_rejects_broad_permissions_symlinks_and_hardlinks(self):
        self.login()
        self.cache.chmod(0o644)
        with self.assertRaises(DriveError) as error:
            auth.TokenProvider(CONFIG, self.cache).account_binding
        self.assertEqual(error.exception.code, "AUTH_CACHE_UNSAFE")
        self.cache.chmod(0o600)
        self.cache.parent.chmod(0o755)
        with self.assertRaises(DriveError):
            auth.TokenProvider(CONFIG, self.cache).token()
        self.cache.parent.chmod(0o700)
        other = self.base / "other.json"
        other.write_text("DO_NOT_MODIFY", encoding="utf-8")
        other.chmod(0o600)
        self.cache.unlink()
        self.cache.symlink_to(other)
        with self.assertRaises(DriveError):
            auth._write_cache(self.cache, {"refresh_token": "SECRET"})
        self.assertEqual(other.read_text(), "DO_NOT_MODIFY")
        self.cache.unlink()
        os.link(other, self.cache)
        with self.assertRaises(DriveError):
            auth._read_cache(self.cache)
        self.assertEqual(other.read_text(), "DO_NOT_MODIFY")

    def test_atomic_publication_failure_preserves_original_and_cleans_temporary(self):
        self.login()
        original = self.cache.read_bytes()
        target = "team_drive.graph_auth.os.replace" if os.name != "nt" else "team_drive.fs_access._rename_same_directory"
        with mock.patch(target, side_effect=OSError("DO_NOT_EXPOSE_SYSTEM_PATH")), self.assertRaises(DriveError) as error:
            auth._write_cache(self.cache, dict(auth._read_cache(self.cache), refresh_token="UPDATED_SECRET"))
        self.assertEqual(error.exception.code, "AUTH_CACHE_UNSAFE")
        self.assertNotIn("DO_NOT_EXPOSE", str(error.exception))
        self.assertEqual(self.cache.read_bytes(), original)
        self.assertEqual(list(self.cache.parent.glob(".auth-*.tmp")), [])

    def test_transport_uses_fixed_official_authority_and_form_data(self):
        opener = mock.Mock()
        opener.open.return_value = FakeResponse({"ok": True})
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(auth._request(TENANT, "token", {"client_id": CLIENT, "scope": "Files.Read"}, 1), {"ok": True})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://login.microsoftonline.com/" + TENANT + "/oauth2/v2.0/token")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(urllib.parse.parse_qs(request.data.decode())["scope"], ["Files.Read"])
        with self.assertRaises(DriveError) as error:
            auth._NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.invalid")
        self.assertEqual(error.exception.code, "AUTH_REDIRECT_BLOCKED")

    def test_transport_error_body_and_oversized_response_are_not_exposed(self):
        opener = mock.Mock()
        opener.open.return_value = FakeResponse({"error": "invalid_grant", "error_description": "SECRET_PRIVATE_DETAIL"}, 400)
        with mock.patch("urllib.request.build_opener", return_value=opener), self.assertRaises(auth._OAuthError) as error:
            auth._request(TENANT, "token", {}, 1)
        self.assertEqual(error.exception.code, "invalid_grant")
        self.assertNotIn("SECRET_PRIVATE_DETAIL", str(error.exception))
        opener.open.return_value = FakeResponse(b"X" * (auth._MAX_BYTES + 1))
        with mock.patch("urllib.request.build_opener", return_value=opener), self.assertRaises(DriveError) as error:
            auth._request(TENANT, "token", {}, 1)
        self.assertEqual(error.exception.code, "AUTH_INVALID_RESPONSE")

    def test_cli_resolves_relative_cache_and_outputs_only_safe_metadata(self):
        config = self.base / "config.json"
        config.write_text(json.dumps({"provider": "graph", "graph": dict(CONFIG, auth_cache="profile/auth.json")}), encoding="utf-8")
        output = io.StringIO()
        with mock.patch.object(auth, "_request", side_effect=[device_response(), token_response()]), \
             mock.patch.object(auth, "_wait"), contextlib.redirect_stdout(output):
            code = auth.main(["--config", str(config)])
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([event["stage"] for event in events], ["device_code", "complete"])
        self.assertNotIn("SYNTHETIC_ACCESS_SECRET", output.getvalue())
        self.assertTrue(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
