"""Exercise the delivered ZIP with system site-packages disabled."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class PackagedSkillTests(unittest.TestCase):
    def test_zip_is_self_contained_and_reuses_an_external_index(self):
        spec = importlib.util.spec_from_file_location("skill_builder", ROOT / "scripts" / "build-skill.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            archive_path = base / "skill.zip"
            built = builder.build(archive_path)
            self.assertLess(built["file_count"], 450)
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                self.assertEqual(names.count("SKILL.md"), 1, "Hub requires SKILL.md at the archive root")
                self.assertEqual(sum(Path(name).name.casefold() == "skill.md" for name in names), 1)
                self.assertFalse(any(name.startswith("df-mcp/") for name in names))
                self.assertFalse(any(name.endswith((".sqlite", ".pyc", "auth.json")) for name in names))
                manifest = json.loads(archive.read("PACKAGE_MANIFEST.json"))
                self.assertEqual(manifest["archive_layout"], "root-skill")
                for entry in manifest["files"]:
                    self.assertEqual(hashlib.sha256(archive.read(entry["path"])).hexdigest(), entry["sha256"])
                archive.extractall(base / "install")
            skill = base / "install"
            env = dict(os.environ, PYTHONPATH="", PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
            def run(script, *args):
                response = subprocess.run([sys.executable, "-S", str(skill / "scripts" / script), *args],
                                          capture_output=True, text=True, encoding="utf-8", env=env, cwd=base, timeout=60)
                self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
                return json.loads(response.stdout)
            # No system-installed parsing dependency is visible with -S.
            check = subprocess.run([sys.executable, "-S", "-c",
                                    "import sys;sys.path.insert(0,sys.argv[1]);from _df_runtime import bootstrap;bootstrap();import pypdf,defusedxml;print(pypdf.__version__)",
                                    str(skill / "scripts")], capture_output=True, text=True, env=env, cwd=base, timeout=20)
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(check.stdout.strip(), "6.21.2")
            source = base / "SIMULATED source"
            source.mkdir()
            (source / "budget.txt").write_text("SIMULATED budget 123\n", encoding="utf-8")
            state = base / "independent index"
            plan = run("df-init.py", "plan", "--choice", "local", "--source", str(source), "--state-dir", str(state))
            self.assertFalse(state.exists(), "plan must be read-only")
            plan_path = base / "reviewed-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            applied = run("df-init.py", "apply", "--plan", str(plan_path), "--confirmed")
            config_path = Path(applied["config_path"])
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["service"] = {"idle_seconds": 1}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            first = second = None
            try:
                first = run("df-tool.py", "status", "--config", str(config_path), "--wait-ready", "20")
                result = run("df-tool.py", "search", "--config", str(config_path), "--params", '{"query":"budget"}', "--paginate")
                self.assertTrue(result["pagination_complete"])
                self.assertEqual(len(result["pages"][0]["files"]), 1)
                second = run("df-tool.py", "status", "--config", str(config_path))
                self.assertEqual(first["instance_id"], second["instance_id"])
                self.assertEqual(first["provider"], "local")
                report = run("df-check.py", "--config", str(config_path), "--confirmed-config", "--query", "budget")
                self.assertTrue(report["ok"])
                self.assertEqual(report["sample"]["technical_result"], "passed")
                self.assertTrue(report["sample"]["same_source_version"])
                self.assertEqual(report["search"]["performance_basis"], "nonempty_index_and_results")
                self.assertTrue(all(report["reuse"].values()))
                for private in (str(source), str(config_path), "SIMULATED budget 123", "budget.txt"):
                    self.assertNotIn(private, json.dumps(report))
                profiles = run("df-init.py", "profiles", "--state-dir", str(state))
                self.assertTrue(profiles["ok"])
                file_id = result["pages"][0]["files"][0]["file_id"]
                inspected = run("df-tool.py", "inspect", "--config", str(config_path),
                                "--params", json.dumps({"file_id": file_id}))
                evidence = run("df-tool.py", "read", "--config", str(config_path),
                               "--params", json.dumps({"file_id": file_id,
                                   "expected_version": inspected["source_version"], "limit": 1}))
                segment = evidence["segments"][0]
                page = {"page_id": "sample-budget", "kind": "source", "title": "Synthetic budget",
                        "body": "The synthetic source records budget 123. [1]", "citations": [{
                            "file_id": file_id, "source_version": inspected["source_version"],
                            "ordinal": segment["ordinal"], "locator": segment["locator"],
                            "quote": "SIMULATED budget 123"}]}
                written = run("df-tool.py", "wiki_write", "--config", str(config_path),
                              "--params", json.dumps(page))
                self.assertTrue(written["ok"])
                wiki = run("df-tool.py", "wiki_read", "--config", str(config_path),
                           "--params", '{"page_id":"sample-budget"}')
                self.assertTrue(wiki["ok"])
                self.assertIn("budget 123", json.dumps(wiki))
                found = run("df-tool.py", "wiki_search", "--config", str(config_path),
                            "--params", '{"query":"budget"}')
                self.assertIn("sample-budget", json.dumps(found))
                self.assertTrue(list(state.rglob("wiki-page-sample-budget.md")))
                self.assertFalse(list(skill.rglob("wiki-page-*.md")))
                self.assertEqual((source / "budget.txt").read_text(encoding="utf-8"), "SIMULATED budget 123\n")
                self.assertFalse(list(source.glob("*.sqlite*")))
                self.assertFalse(list(skill.rglob("*.sqlite*")))
            finally:
                endpoint = Path(config["state_dir"]) / "shared-v1" / "control" / "endpoint.json"
                deadline = time.monotonic() + 8
                while endpoint.exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertFalse(endpoint.exists(), "test broker should finish its idle shutdown")


if __name__ == "__main__":
    unittest.main()
