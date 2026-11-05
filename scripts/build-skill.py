"""Build a portable, allowlisted Skill ZIP; never copy user state or credentials."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = {"pypdf": ("6.21.2", {"pypdf"}), "defusedxml": ("0.7.1", {"defusedxml"}),
                "typing_extensions": ("4.16.0", {"typing_extensions.py"})}
SCRIPTS = ("_df_runtime.py", "df-tool.py", "df-init.py", "df-cloud.py", "df-check.py")


def build(output):
    version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    files = {}

    def add(name, path):
        if path.is_symlink() or not path.is_file():
            raise ValueError("Package sources must be regular files: " + str(path))
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or name in files:
            raise ValueError("Invalid or duplicated package member: " + name)
        files[name] = path.read_bytes()

    for path in sorted((ROOT / "skills" / "df-mcp").rglob("*")):
        if path.is_dir():
            continue
        if path.suffix not in (".md", ".yaml", ".json", ".txt"):
            raise ValueError("Unexpected skill resource: " + str(path))
        add(path.relative_to(ROOT / "skills" / "df-mcp").as_posix(), path)
    for name in SCRIPTS:
        add("scripts/" + name, ROOT / "scripts" / name)
    for path in sorted((ROOT / "python" / "team_drive").glob("*.py")):
        add("runtime/python/team_drive/" + path.name, path)
    add("runtime/requirements.txt", ROOT / "requirements.txt")
    add("LICENSE", ROOT / "LICENSE")
    vendors = []
    for name, (version_pin, modules) in DEPENDENCIES.items():
        distribution = importlib.metadata.distribution(name)
        if distribution.version != version_pin:
            raise ValueError("Build requires the pinned dependency " + name + "==" + version_pin)
        count = 0
        for entry in sorted(distribution.files or [], key=str):
            parts = PurePosixPath(str(entry)).parts
            if not parts or ".." in parts or "__pycache__" in parts or str(entry).endswith(".pyc"):
                continue
            is_module = parts[0] in modules
            is_metadata = parts[0].endswith(".dist-info") and (
                parts[-1] in {"METADATA", "WHEEL", "top_level.txt"} or
                any("license" in part.lower() or "copying" in part.lower() for part in parts[1:]))
            if is_module or is_metadata:
                add("runtime/vendor/" + PurePosixPath(str(entry)).as_posix(), Path(distribution.locate_file(entry)))
                count += 1
        if not count:
            raise ValueError("No dependency files found for " + name)
        vendors.append({"name": name, "version": distribution.version, "files": count})
    if "SKILL.md" not in files or sum(PurePosixPath(name).name.casefold() == "skill.md" for name in files) != 1:
        raise ValueError("The package must contain exactly one root-level SKILL.md.")
    manifest = {"schema": "df-mcp-skill-package-v1", "version": version, "python_minimum": "3.9",
                "archive_layout": "root-skill",
                "includes_python_interpreter": False, "source_documents_included": False,
                "credentials_included": False, "index_included": False, "dependencies": vendors,
                "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in sorted(files.items())]}
    files["PACKAGE_MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(files) > 450 or sum(map(len, files.values())) > 20 * 1024 * 1024:
        raise ValueError("Package exceeds the project's conservative file/size budget.")
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally replacing a reviewed release.
    with output.open("xb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            # The company Hub reads SKILL.md at the archive root and creates
            # the installation directory itself; do not wrap it in df-mcp/.
            member = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o100644 << 16
            archive.writestr(member, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {"ok": True, "path": str(output), "version": version, "file_count": len(files),
            "bytes": output.stat().st_size, "sha256": digest, "vendors": vendors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False, indent=2))
