"""Inspect or explicitly select configured sources without connecting to them."""
import sys

sys.dont_write_bytecode = True
from _df_runtime import bootstrap

bootstrap()
from team_drive.catalog import main

if __name__ == "__main__":
    raise SystemExit(main())
