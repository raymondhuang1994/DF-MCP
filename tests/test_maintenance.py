"""Preview/apply and live lease exclusion for bounded state reclamation."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from team_drive.engine import Engine
from team_drive.errors import DriveError
from team_drive.maintenance import maintain
from team_drive.fs_access import SourceAccess, StateAccess


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-mcp-maintenance-")
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.fixture = self.source / "evidence.txt"
        self.fixture.write_text("SIMULATED maintenance evidence", encoding="utf-8")
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"root": str(self.source), "state_dir": str(self.base / "state"),
                                           "state_pool": {"max_slots": 2}}), encoding="utf-8")
        self.engine = Engine(str(self.config), autostart=False, pooled=True)
        self.engine.scan_once()
        self.slot = self.engine.state

    def tearDown(self):
        self.engine.close()
        self.assertEqual(self.fixture.read_text(encoding="utf-8"), "SIMULATED maintenance evidence")
        self.temp.cleanup()

    def test_live_slot_is_skipped_even_with_explicit_apply(self):
        result = maintain(str(self.config), apply=True, reset_index=True, prune_exports_days=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["busy_slots_skipped"], 1)
        self.assertEqual(result["slots"][0]["status"], "busy")
        self.assertTrue(self.engine.status()["ok"])
        self.assertTrue((self.slot / "index.sqlite").exists())
        self.assertFalse((self.slot.parent / "slot-02").exists(), "Maintenance must not create unallocated slots.")

    def test_preview_then_reset_and_export_expiry_preserves_unknown_files(self):
        file = self.engine.list()["files"][0]
        exported = Path(self.engine.export(file["file_id"], file["source_version"])["artifact_path"])
        self.engine.close()
        os.utime(exported, (0, 0))
        unknown = exported.parent / "keep-me.txt"
        unknown.write_text("user-maintained file", encoding="utf-8")
        scratch = self.slot / ("job-temp-" + "a" * 32)
        scratch.mkdir()
        snapshot = scratch / ("snapshot-" + "b" * 32 + ".txt")
        snapshot.write_text("temporary snapshot", encoding="utf-8")
        before = (self.slot / "index.sqlite").read_bytes()
        preview = maintain(str(self.config), reset_index=True, prune_exports_days=7)
        self.assertTrue(preview["ok"], preview)
        self.assertFalse(preview["applied"])
        self.assertEqual(preview["slots"][0]["export_files"], 1)
        self.assertEqual(preview["slots"][0]["scratch"]["eligible_files"], 1)
        self.assertEqual((self.slot / "index.sqlite").read_bytes(), before)
        self.assertTrue(exported.exists())
        self.assertTrue(snapshot.exists())
        result = maintain(str(self.config), apply=True, reset_index=True, prune_exports_days=7)
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.slot / "index.sqlite").exists())
        self.assertFalse(exported.exists())
        self.assertFalse(scratch.exists())
        self.assertEqual(unknown.read_text(encoding="utf-8"), "user-maintained file")
        self.assertTrue((self.slot / ".worker.lock").exists())
        self.engine = Engine(str(self.config), autostart=False, pooled=True)
        self.engine.scan_once()
        self.assertTrue(self.engine.status()["coverage"]["all_present_files_indexed"])

    def test_exports_are_preserved_without_explicit_age_option(self):
        file = self.engine.list()["files"][0]
        exported = Path(self.engine.export(file["file_id"], file["source_version"])["artifact_path"])
        self.engine.close()
        os.utime(exported, (0, 0))
        result = maintain(str(self.config), apply=True, reset_index=True)
        self.assertTrue(result["ok"])
        self.assertTrue(exported.exists())

    def test_corrupt_owned_index_is_quarantined_and_can_be_reclaimed(self):
        self.engine.close()
        (self.slot / "index.sqlite").write_bytes(b"SIMULATED damaged database" * 20)
        self.engine = Engine(str(self.config), autostart=False, pooled=True)
        self.assertTrue((self.slot / "index.corrupt.sqlite").exists())
        self.engine.close()
        result = maintain(str(self.config), apply=True, reset_index=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["busy_slots_skipped"], 0, "Failed startup must release its slot.")
        self.engine = Engine(str(self.config), autostart=False, pooled=True)
        self.engine.scan_once()
        self.assertTrue(self.engine.status()["coverage"]["all_present_files_indexed"])

    def test_lowered_capacity_still_finds_old_slots(self):
        second = Engine(str(self.config), autostart=False, pooled=True)
        second.scan_once()
        second_slot = second.state
        second.close()
        self.engine.close()
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["state_pool"]["max_slots"] = 1
        self.config.write_text(json.dumps(config), encoding="utf-8")
        result = maintain(str(self.config), apply=True, reset_index=True)
        self.assertEqual({row["slot"] for row in result["slots"]}, {1, 2})
        self.assertFalse((second_slot / "index.sqlite").exists())

    def test_missing_source_cleanup_uses_recorded_manifest_without_source_writes(self):
        self.engine.close()
        saved = self.base / "temporarily-offline-source"
        self.source.rename(saved)
        try:
            result = maintain(str(self.config), apply=True, reset_index=True)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["slots"][0]["source_missing"])
            self.assertFalse((self.slot / "index.sqlite").exists())
            self.assertEqual((saved / self.fixture.name).read_text(encoding="utf-8"), "SIMULATED maintenance evidence")
        finally:
            saved.rename(self.source)

    def test_replaced_source_requires_explicit_reset_acceptance(self):
        self.engine.close()
        saved = self.base / "original-source"
        self.source.rename(saved)
        self.source.mkdir()
        (self.source / "new.txt").write_text("replacement", encoding="utf-8")
        try:
            rejected = maintain(str(self.config), apply=True, reset_index=True)
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["slots"][0]["error"]["code"], "STATE_ROOT_MISMATCH")
            self.assertTrue((self.slot / "index.sqlite").exists())
            accepted = maintain(str(self.config), apply=True, reset_index=True, accept_source_replacement=True)
            self.assertTrue(accepted["ok"], accepted)
            self.assertTrue(accepted["slots"][0]["source_replacement_accepted"])
            self.assertFalse((self.slot / "index.sqlite").exists())
        finally:
            (self.source / "new.txt").unlink()
            self.source.rmdir()
            saved.rename(self.source)

    def test_shared_control_lease_prevents_reset_during_broker_startup(self):
        self.engine.close()
        state_base = self.base / "state"
        shared = Engine(str(self.config), autostart=False, state_override=state_base / "shared-v1" / "index")
        shared.scan_once()
        shared.close()
        source = SourceAccess(self.source)
        control = StateAccess(state_base / "shared-v1" / "control", source, Path(__file__).resolve().parents[1])
        control.lock()
        try:
            result = maintain(str(self.config), apply=True, reset_index=True)
            row = next(row for row in result["slots"] if row["slot"] == "shared")
            self.assertEqual(row["status"], "busy")
            self.assertTrue((shared.state / "index.sqlite").exists())
        finally:
            control.close()
            source.close()

    def test_retained_bundle_pins_export_during_age_cleanup(self):
        file = self.engine.list()["files"][0]
        bundle = self.engine.evidence_bundle([{"file_id": file["file_id"], "expected_version": file["source_version"]}])
        manifest_path = Path(bundle["artifact_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        exported = Path(manifest["files"][0]["artifact_path"])
        self.engine.close()
        os.utime(exported, (0, 0))
        result = maintain(str(self.config), apply=True, prune_exports_days=0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["slots"][0]["bundle_exports_preserved"], 1)
        self.assertTrue(exported.exists())
        self.assertTrue(manifest_path.exists())

    def test_unsafe_state_and_redirected_export_are_never_deleted(self):
        file = self.engine.list()["files"][0]
        exported = Path(self.engine.export(file["file_id"], file["source_version"])["artifact_path"])
        self.engine.close()
        exported.unlink()
        os.link(self.fixture, exported)
        result = maintain(str(self.config), apply=True, prune_exports_days=0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["slots"][0]["error"]["code"], "UNSAFE_STATE")
        self.assertTrue(exported.exists())
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["state_dir"] = str(self.base / ".codex" / "state")
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(DriveError) as caught:
            maintain(str(self.config), apply=True, reset_index=True)
        self.assertEqual(caught.exception.code, "UNSAFE_STATE")
        self.assertFalse((self.base / ".codex").exists())


if __name__ == "__main__":
    unittest.main()
