# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Direct Launcher script
"""

import os
import sys
from pathlib import Path

src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

# pylint: disable=wrong-import-position,import-outside-toplevel
from mldebug.mldebug_cli import app

if __name__ == "__main__":
  os.environ.setdefault("ENABLE_DEV", "1")
  print(
    f"{'*' * 40}\n"
    "[WARNING] This launcher script is for dev use.\n"
    "The recommended flow is to install the wheel and use 'mldebug' command.\n"
    f"{'*' * 40}\n"
  )
  app()
