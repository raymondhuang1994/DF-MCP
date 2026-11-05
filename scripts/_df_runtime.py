"""Locate the same reader in a source checkout or a self-contained Skill ZIP."""
import os
from pathlib import Path
import sys


def bootstrap():
    if sys.version_info < (3, 9):
        raise RuntimeError("DF 资料助手需要 Python 3.9 或更新版本，请使用公司批准的运行环境。")
    root = Path(__file__).resolve().parents[1]
    package = root / "runtime" / "python"
    if not package.is_dir():
        package = root / "python"
    if not (package / "team_drive" / "__init__.py").is_file():
        raise RuntimeError("工具包不完整：缺少读取程序，请重新导入完整 ZIP。")
    vendor = root / "runtime" / "vendor"
    paths = [str(package)] + ([str(vendor)] if vendor.is_dir() else [])
    sys.path[:0] = paths
    inherited = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join(paths + ([inherited] if inherited else []))
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    return root
