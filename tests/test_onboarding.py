"""Synthetic, offline onboarding tests; no real shares, tenant or user data."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from team_drive import onboarding as init
from team_drive import graph_auth as auth
from team_drive.errors import DriveError


TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ACCOUNT = "33333333-3333-3333-3333-333333333333"
SELECTION = {"provider": "microsoft_graph", "drive_id": "drive-id", "folder_id": "folder-id",
             "name": "Digital Finance", "web_url": "https://example.sharepoint.com/sites/Finance/Documents/Digital%20Finance"}


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-onboarding-")
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "Digital Finance"
        self.source.mkdir()
        (self.source / "evidence.txt").write_text("synthetic source remains unchanged", encoding="utf-8")
        self.state = self.base / "appdata"
        self.no_network = mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No real network in onboarding tests"))
        self.no_network.start()

    def tearDown(self):
        self.no_network.stop()
        self.temp.cleanup()

    def plan(self, **changes):
        return init.make_plan("local", str(self.source), state_dir=str(self.state), **changes)

    def authorize(self):
        plan = init.account_plan(TENANT, CLIENT, state_dir=str(self.state))
        cache = {"version": 1, "authority": auth._AUTHORITY, "tenant_selector": TENANT,
                 "client_id": CLIENT, "scopes": ["Files.Read"], "tenant_id": TENANT, "object_id": ACCOUNT,
                 "account_binding": auth._account_binding(TENANT, ACCOUNT, CLIENT),
                 "refresh_token": "SYNTHETIC_REFRESH_SECRET"}
        auth._write_cache(plan["auth_cache"], cache)
        return plan

    def cloud_plan(self, **changes):
        args = {"selection": SELECTION, "tenant_id": TENANT, "client_id": CLIENT, "state_dir": str(self.state)}
        args.update(changes)
        return init.make_plan("team", **args)

    def test_plan_is_read_only_and_confirmed_apply_creates_only_configuration(self):
        plan = self.plan()
        self.assertFalse(self.state.exists())
        with self.assertRaises(DriveError) as failure:
            init.apply_plan(plan)
        self.assertEqual(failure.exception.code, "CONFIRMATION_REQUIRED")
        self.assertFalse(self.state.exists())
        applied = init.apply_plan(plan, confirmed=True)
        config = Path(applied["config_path"])
        self.assertEqual(json.loads(config.read_text()), plan["config"])
        self.assertEqual(json.loads(config.read_text())["approved_source_identity"], plan["source_identity"])
        self.assertFalse(Path(plan["config"]["state_dir"]).exists())
        self.assertEqual([path.name for path in config.parent.iterdir()], ["config.json"])
        self.assertEqual((self.source / "evidence.txt").read_text(), "synthetic source remains unchanged")
        self.assertFalse(applied["index_created"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config.parent.stat().st_mode), 0o700)

    def test_reuse_preserves_existing_configuration_bytes_and_runtime_settings(self):
        plan = self.plan()
        init.apply_plan(plan, confirmed=True)
        path = Path(plan["config_path"])
        config = json.loads(path.read_text())
        config["poll_seconds"] = 90
        path.write_text(json.dumps(config, indent=4) + "\n")
        before = path.read_bytes()
        self.assertTrue(init.apply_plan(plan, confirmed=True)["reused"])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(plan["config"]["state_dir"]).exists())

    def test_existing_binding_is_never_overwritten(self):
        plan = self.plan()
        init.apply_plan(plan, confirmed=True)
        path = Path(plan["config_path"])
        config = json.loads(path.read_text())
        other = self.base / "another-source"
        other.mkdir()
        config["root"] = str(other)
        path.write_text(json.dumps(config))
        before = path.read_bytes()
        with self.assertRaises(DriveError) as failure:
            init.apply_plan(plan, confirmed=True)
        self.assertEqual(failure.exception.code, "CONFIG_BINDING_MISMATCH")
        self.assertEqual(path.read_bytes(), before)

    def test_reuse_rejects_missing_or_changed_approved_source_identity(self):
        plan = self.plan()
        init.apply_plan(plan, confirmed=True)
        path = Path(plan["config_path"])
        original = json.loads(path.read_text())
        for identity in (None, dict(original["approved_source_identity"], inode=original["approved_source_identity"]["inode"] + 1)):
            changed = dict(original)
            if identity is None:
                changed.pop("approved_source_identity")
            else:
                changed["approved_source_identity"] = identity
            path.write_text(json.dumps(changed))
            before = path.read_bytes()
            with self.subTest(identity=identity), self.assertRaises(DriveError) as failure:
                init.apply_plan(plan, confirmed=True)
            self.assertEqual(failure.exception.code, "CONFIG_BINDING_MISMATCH")
            self.assertEqual(path.read_bytes(), before)

    def test_plan_tampering_is_recomputed_before_writes(self):
        original = self.plan()
        for field, value in (("config_path", str(self.source / "bad.json")), ("source", "changed"),
                             ("plan_id", "forged"), ("version", 2), ("index_created", True)):
            plan = copy.deepcopy(original)
            plan[field] = value
            with self.subTest(field=field), self.assertRaises(DriveError):
                init.apply_plan(plan, confirmed=True)
            self.assertFalse(self.state.exists())
        plan = copy.deepcopy(original)
        plan["config"]["state_dir"] = str(self.source / "index")
        with self.assertRaises(DriveError):
            init.apply_plan(plan, confirmed=True)
        self.assertFalse(self.state.exists())

    def test_unknown_plan_fields_and_non_boolean_confirmation_are_rejected(self):
        for plan in (dict(self.plan(), extra=True), {"config": self.plan()["config"]}):
            with self.assertRaises(DriveError):
                init.apply_plan(plan, confirmed=True)
        with self.assertRaises(DriveError):
            init.apply_plan(self.plan(), confirmed="true")
        self.assertFalse(self.state.exists())

    def test_source_state_installation_and_codex_overlaps_are_rejected(self):
        for state in (self.source / "state", self.base, init.installation_root() / "state", self.base / ".codex" / "state"):
            with self.subTest(state=str(state)), self.assertRaises(DriveError):
                init.make_plan("local", str(self.source), state_dir=str(state))
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.base / "custom-codex")}):
            with self.assertRaises(DriveError):
                init.make_plan("local", str(self.source), state_dir=str(self.base / "custom-codex" / "state"))
        with self.assertRaises(DriveError):
            init.make_plan("local", str(init.installation_root()), state_dir=str(self.state))

    @unittest.skipIf(os.name == "nt", "Creating test symlinks requires separate Windows privileges")
    def test_symlink_ancestors_and_hardlinked_existing_config_are_rejected(self):
        target = self.base / "target"
        target.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(DriveError):
            init.make_plan("local", str(self.source), state_dir=str(alias / "state"))
        source_alias = self.base / "source-alias"
        source_alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(DriveError):
            init.make_plan("local", str(source_alias), state_dir=str(self.state))
        plan = self.plan()
        init.apply_plan(plan, confirmed=True)
        os.link(plan["config_path"], self.base / "config-link.json")
        with self.assertRaises(DriveError):
            init.apply_plan(plan, confirmed=True)

    def test_missing_source_does_not_create_anything(self):
        with self.assertRaises(DriveError):
            init.make_plan("local", str(self.base / "missing"), state_dir=str(self.state))
        self.assertFalse(self.state.exists())

    def test_existing_state_file_or_non_private_profile_is_rejected(self):
        plan = self.plan()
        profile = Path(plan["config_path"]).parent
        profile.mkdir(parents=True, mode=0o700)
        state = Path(plan["config"]["state_dir"])
        state.write_text("must not be overwritten")
        with self.assertRaises(DriveError):
            init.apply_plan(plan, confirmed=True)
        self.assertEqual(state.read_text(), "must not be overwritten")
        self.assertFalse(Path(plan["config_path"]).exists())
        state.unlink()
        if os.name != "nt":
            profile.chmod(0o755)
            with self.assertRaises(DriveError):
                self.plan()

    def test_classifies_cloud_url_but_rejects_short_links_and_credentials(self):
        for source in (SELECTION["web_url"], "https://onedrive.live.com/?id=abc&cid=def"):
            self.assertEqual(init.classify(source)["provider"], "microsoft_graph")
        for source in ("https://1drv.ms/f/abc", "https://example.com/folder", "http://example.sharepoint.com/folder",
                       "https://user:secret" + "@" + "example.sharepoint.com/folder", "file:///tmp/source"):
            with self.subTest(source=source), self.assertRaises(DriveError):
                init.classify(source)
        with self.assertRaises(DriveError) as failure:
            init.make_plan("team", SELECTION["web_url"], state_dir=str(self.state))
        self.assertEqual(failure.exception.code, "CLOUD_SELECTION_REQUIRED")
        self.assertFalse(self.state.exists())

    def test_unc_is_not_treated_as_a_local_directory(self):
        result = init.classify(r"\\server\Digital Finance")
        self.assertEqual(result["provider"], "windows_share")
        self.assertEqual(result["kind"], "unc")
        if os.name != "nt":
            with self.assertRaises(DriveError) as failure:
                init.make_plan("team", result["source"], state_dir=str(self.state))
            self.assertEqual(failure.exception.code, "UNSUPPORTED_PLATFORM")
            self.assertTrue(init.classify(r"Z:\Digital Finance")["requires_drive_type_check"])

    def test_windows_mapped_drive_classification_uses_drive_remote(self):
        # Patch only the classification platform switch; no Windows filesystem I/O.
        with mock.patch.object(init.os, "name", "nt"), mock.patch.object(init, "_windows_drive_type", return_value=4) as drive_type:
            result = init.classify(r"Z:\Digital Finance")
        drive_type.assert_called_once_with(r"Z:\Digital Finance")
        self.assertEqual(result["provider"], "windows_share")
        self.assertEqual(result["drive_type"], 4)

    def test_mapped_drive_remapping_requires_a_new_reviewed_plan(self):
        classification = {"provider": "windows_share", "source": "Z:\\Digital Finance",
                          "supported_on_this_platform": True}
        identity = {"provider": "windows_share", "unc": r"\\server-a\Digital Finance",
                    "target": r"\\server-a\Digital Finance", "identity": [1, 2], "principal": "synthetic-user"}
        with mock.patch.object(init, "classify", return_value=classification), \
                mock.patch.object(init, "_source_check", return_value={"root": r"\\server-a\Digital Finance", "identity": identity}):
            plan = init.make_plan("team", classification["source"], state_dir=str(self.state))
        self.assertEqual(plan["resolved_root"], r"\\server-a\Digital Finance")
        with mock.patch.object(init, "classify", return_value=classification), \
                mock.patch.object(init, "_source_check", return_value={"root": r"\\server-b\Digital Finance", "identity": identity}):
            with self.assertRaises(DriveError) as failure:
                init.apply_plan(plan, confirmed=True)
        self.assertEqual(failure.exception.code, "PLAN_CHANGED")
        self.assertFalse(self.state.exists())

    def test_same_path_replacement_requires_new_confirmation(self):
        plan = self.plan()
        before = self.source.stat()
        original = self.base / "renamed-original"
        self.source.rename(original)
        self.source.mkdir()
        (self.source / "replacement.txt").write_text("different source, same spelling")
        self.assertNotEqual(before.st_ino, self.source.stat().st_ino)
        with self.assertRaises(DriveError) as failure:
            init.apply_plan(plan, confirmed=True)
        self.assertEqual(failure.exception.code, "PLAN_CHANGED")
        self.assertFalse(self.state.exists())
        fresh = self.plan()
        self.assertNotEqual(fresh["source_identity"], plan["source_identity"])
        self.assertNotEqual(fresh["config_path"], plan["config_path"])

    def test_share_target_identity_or_principal_change_requires_new_confirmation(self):
        source = r"\\company\Digital Finance"
        classification = {"provider": "windows_share", "source": source, "supported_on_this_platform": True}
        identity = {"provider": "windows_share", "unc": source, "target": r"\\target-a\Digital Finance",
                    "identity": [10, 20], "principal": "synthetic-user-a"}
        with mock.patch.object(init, "classify", return_value=classification), \
                mock.patch.object(init, "_source_check", return_value={"root": source, "identity": identity}):
            plan = init.make_plan("team", source, state_dir=str(self.state))
        self.assertEqual(plan["config"]["approved_source_identity"], identity)
        for key, value in (("target", r"\\target-b\Digital Finance"), ("identity", [10, 21]),
                           ("principal", "synthetic-user-b")):
            changed = dict(identity, **{key: value})
            with self.subTest(key=key), mock.patch.object(init, "classify", return_value=classification), \
                    mock.patch.object(init, "_source_check", return_value={"root": source, "identity": changed}):
                with self.assertRaises(DriveError) as failure:
                    init.apply_plan(plan, confirmed=True)
            self.assertEqual(failure.exception.code, "PLAN_CHANGED")
            self.assertFalse(self.state.exists())

    def test_share_preflight_uses_actual_provider_binding_without_walking(self):
        binding = {"provider": "windows_share", "unc": r"\\server\team", "target": r"\\target\team",
                   "identity": [1, 2], "principal": "synthetic-user"}
        access = mock.Mock(root=r"\\server\team", binding=binding)
        with mock.patch("team_drive.network_source.WindowsShareSource", return_value=access):
            result = init._source_check(r"Z:\team", "windows_share")
        self.assertEqual(result, {"root": r"\\server\team", "identity": binding})
        access.check_root.assert_called_once_with()
        access.walk.assert_not_called()
        access.close.assert_called_once_with()

    def test_probe_matches_only_known_names_and_does_not_select_ambiguously(self):
        with mock.patch.object(init, "_connected_windows_resources", return_value=[]), \
                mock.patch.object(init, "_sync_roots", return_value=[self.base]), \
                mock.patch.object(init.Path, "home", return_value=self.base / "empty-home"):
            before = sorted(self.base.rglob("*"))
            result = init.probe()
            self.assertEqual(result["suggested_source"], str(self.source))
            self.assertFalse(result["files_created"])
            self.assertEqual(sorted(self.base.rglob("*")), before)
            (self.base / "Digital Finance_0").mkdir()
            result = init.probe()
            self.assertIsNone(result["suggested_source"])
            self.assertTrue(result["selection_required"])

    def test_probe_reports_mapping_failure_without_fabricating_source(self):
        with mock.patch.object(init, "_connected_windows_resources", side_effect=DriveError("DISCOVERY_UNAVAILABLE", "synthetic failure")), \
                mock.patch.object(init, "_sync_roots", return_value=[]), \
                mock.patch.object(init.Path, "home", return_value=self.base / "empty-home"):
            result = init.probe()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["warnings"][0]["code"], "DISCOVERY_UNAVAILABLE")

    def test_profiles_are_read_only_and_reuse_local_and_cloud_metadata_without_auth(self):
        self.assertEqual(init.profiles(str(self.state))["profiles"], [])
        self.assertFalse(self.state.exists())
        local = self.plan()
        init.apply_plan(local, confirmed=True)
        self.authorize()
        cloud = self.cloud_plan()
        with mock.patch("team_drive.graph_discovery.GraphDiscovery.get_folder", return_value=SELECTION):
            init.apply_plan(cloud, confirmed=True)
        shutil.rmtree(self.source)
        with mock.patch.object(auth.TokenProvider, "__init__", side_effect=AssertionError("No auth read")), \
                mock.patch.object(init, "_source_check", side_effect=AssertionError("No source check")):
            result = init.profiles(str(self.state))
        self.assertEqual({item["provider"] for item in result["profiles"]}, {"local", "microsoft_graph"})
        self.assertFalse(result["sources_checked"])
        self.assertNotIn("SYNTHETIC_REFRESH_SECRET", json.dumps(result))
        self.assertTrue(all(item["source_availability"] == "not_checked" for item in result["profiles"]))
        self.assertFalse(Path(local["config"]["state_dir"]).exists())

    def test_profiles_skip_invalid_config_and_bound_directory_enumeration(self):
        for index in range(130):
            profile = self.state / "profiles" / str(index)
            profile.mkdir(parents=True, mode=0o700)
            (profile / "config.json").write_text("[]")
            if os.name != "nt":
                (profile / "config.json").chmod(0o600)
        result = init.profiles(str(self.state))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["profiles"], [])
        self.assertEqual(len(result["warnings"]), 128)

    def test_cloud_account_plan_has_no_source_ids_or_side_effects(self):
        plan = init.account_plan("organizations", CLIENT, state_dir=str(self.state))
        self.assertEqual(plan["auth_config"]["scopes"], ["Files.Read"])
        self.assertNotIn("drive_id", plan["auth_config"])
        self.assertNotIn("folder_id", plan["auth_config"])
        self.assertFalse(self.state.exists())
        with self.assertRaises(DriveError):
            init.cloud_login(TENANT, CLIENT, state_dir=str(self.state))
        self.assertFalse(self.state.exists())

    def test_cloud_login_saves_only_account_config_and_calls_explicit_login(self):
        events = []

        def login(config, cache, emit):
            self.assertNotIn("drive_id", config)
            self.assertTrue(Path(cache).parent.is_dir())
            emit({"stage": "device_code", "user_code": "SYNTHETIC-CODE"})
            return {"authenticated": True, "cache_saved": True}

        with mock.patch.object(auth, "device_login", side_effect=login) as device:
            result = init.cloud_login(TENANT, CLIENT, state_dir=str(self.state), confirmed=True, emit=events.append)
        device.assert_called_once()
        self.assertTrue(result["authenticated"])
        self.assertFalse(result["source_configured"])
        config = json.loads(Path(result["account_config_path"]).read_text())
        self.assertEqual(set(config), {"graph"})
        self.assertFalse((self.state / "profiles").exists())
        self.assertEqual(len(events), 1)

    def test_missing_auth_never_produces_cloud_source_plan(self):
        with self.assertRaises(DriveError) as failure:
            self.cloud_plan()
        self.assertEqual(failure.exception.code, "AUTH_REQUIRED")
        self.assertFalse(self.state.exists())

    def test_cloud_plan_does_not_refresh_or_write_and_apply_verifies_folder(self):
        account = self.authorize()
        original_cache = Path(account["auth_cache"]).read_bytes()
        plan = self.cloud_plan(selection=dict(SELECTION, ok=True))
        self.assertNotIn("approved_source_identity", plan["config"])
        self.assertEqual(Path(account["auth_cache"]).read_bytes(), original_cache)
        self.assertFalse((self.state / "profiles").exists())
        self.assertNotIn(SELECTION["web_url"], plan["config"]["graph"]["drive_id"])
        with mock.patch("team_drive.graph_discovery.GraphDiscovery.get_folder", return_value=SELECTION) as verify:
            applied = init.apply_plan(plan, confirmed=True)
        verify.assert_called_once_with("drive-id", "folder-id")
        self.assertTrue(Path(plan["config"]["root"]).is_dir())
        self.assertEqual(list(Path(plan["config"]["root"]).iterdir()), [])
        self.assertFalse(Path(plan["config"]["state_dir"]).exists())
        self.assertTrue(Path(applied["config_path"]).is_file())

    def test_cloud_selection_wrong_shape_fake_url_or_failed_result_is_rejected(self):
        self.authorize()
        for selection in (dict(SELECTION, ok=False), dict(SELECTION, unexpected=True),
                          dict(SELECTION, drive_id=SELECTION["web_url"]), dict(SELECTION, web_url=str(self.source))):
            with self.subTest(selection=selection), self.assertRaises(DriveError):
                self.cloud_plan(selection=selection)
        self.assertFalse((self.state / "profiles").exists())

    def test_cloud_apply_fails_on_permission_or_folder_change_without_source_profile(self):
        self.authorize()
        plan = self.cloud_plan()
        with mock.patch("team_drive.graph_discovery.GraphDiscovery.get_folder", side_effect=DriveError("GRAPH_FORBIDDEN", "Denied")):
            with self.assertRaises(DriveError):
                init.apply_plan(plan, confirmed=True)
        with mock.patch("team_drive.graph_discovery.GraphDiscovery.get_folder", return_value=dict(SELECTION, folder_id="different")):
            with self.assertRaises(DriveError) as failure:
                init.apply_plan(plan, confirmed=True)
        self.assertEqual(failure.exception.code, "SOURCE_CHANGED")
        self.assertFalse((self.state / "profiles").exists())

    def test_cli_reports_json_errors_and_rejects_unconfirmed_apply(self):
        for args in (["plan"], ["bad-command"], ["classify", "--source", "https://example.com"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                result = init.main(args)
            self.assertEqual(result, 1)
            self.assertFalse(json.loads(out.getvalue())["ok"])
        path = self.base / "plan.json"
        path.write_text(json.dumps(self.plan()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = init.main(["apply", "--plan", str(path)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], "CONFIRMATION_REQUIRED")
        self.assertFalse(self.state.exists())

    def test_python_launcher_works_in_repository_and_skill_zip_layout(self):
        repository = Path(__file__).resolve().parents[1]
        packaged = self.base / "skill"
        (packaged / "scripts").mkdir(parents=True)
        (packaged / "runtime").mkdir()
        shutil.copy(repository / "scripts" / "df-init.py", packaged / "scripts" / "df-init.py")
        shutil.copy(repository / "scripts" / "_df_runtime.py", packaged / "scripts" / "_df_runtime.py")
        shutil.copytree(repository / "python" / "team_drive", packaged / "runtime" / "python" / "team_drive",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for index, script in enumerate((repository / "scripts" / "df-init.py", packaged / "scripts" / "df-init.py")):
            command = [sys.executable, str(script), "classify", "--source", str(self.source)]
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["provider"], "local")
            state = self.base / ("cli-state-%d" % index)
            command = [sys.executable, str(script), "plan", "--choice", "local", "--source", str(self.source),
                       "--state-dir", str(state)]
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertFalse(state.exists())
            plan = json.loads(result.stdout)
            plan_file = self.base / ("cli-plan-%d.json" % index)
            plan_file.write_text(json.dumps(plan))
            command = [sys.executable, str(script), "apply", "--plan", str(plan_file), "--confirmed"]
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(json.loads(result.stdout)["ok"])
            self.assertFalse(Path(plan["config"]["state_dir"]).exists())
            command = [sys.executable, str(script), "profiles", "--state-dir", str(state)]
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["profiles"][0]["config_path"], plan["config_path"])
        self.assertEqual(list(packaged.rglob("__pycache__")), [])


if __name__ == "__main__":
    unittest.main()
