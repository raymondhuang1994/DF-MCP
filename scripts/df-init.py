#!/usr/bin/env python3
"""Run onboarding from the repository or the complete Skill ZIP without Node."""
import sys

sys.dont_write_bytecode = True
from _df_runtime import bootstrap

bootstrap()

from team_drive.onboarding import main

if __name__ == "__main__":
    raise SystemExit(main())
