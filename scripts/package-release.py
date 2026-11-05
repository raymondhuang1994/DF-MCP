"""Build identical ZIP/.skill installation files plus a SHA-256 manifest."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


def package(output_dir):
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = [output / ("DF-MCP-" + version + suffix) for suffix in (".zip", ".skill")]
    checksum = output / "SHA256SUMS.txt"
    if any(path.exists() for path in paths + [checksum]):
        raise FileExistsError("Release output exists; use a new empty output directory.")
    spec = importlib.util.spec_from_file_location("skill_builder", root / "scripts" / "build-skill.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    result = builder.build(paths[0])
    payload = paths[0].read_bytes()
    with paths[1].open("xb") as stream:
        stream.write(payload)
    digest = hashlib.sha256(payload).hexdigest()
    with checksum.open("x", encoding="utf-8", newline="\n") as stream:
        stream.writelines(digest + "  " + path.name + "\n" for path in paths)
    return dict(result, skill_path=str(paths[1]), checksum_path=str(checksum))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    print(json.dumps(package(parser.parse_args().output_dir), ensure_ascii=False, indent=2))
