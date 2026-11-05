#!/usr/bin/env python3
"""Run bounded acceptance checks for an already confirmed DF configuration."""
import sys

sys.dont_write_bytecode = True
from _df_runtime import bootstrap

bootstrap()

from team_drive.acceptance import main

if __name__ == "__main__":
    raise SystemExit(main())
