"""Invoke an evidence tool from the checkout or packaged Skill."""
import sys

sys.dont_write_bytecode = True

from _df_runtime import bootstrap

bootstrap()

from team_drive.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
