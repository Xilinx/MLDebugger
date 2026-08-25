# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Stamps the git commit into the built package. All metadata lives in pyproject.toml.
"""

from datetime import datetime, timezone
from pathlib import Path

import os
import subprocess

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).parent.resolve()


def _git(*args):
  """
  Run a git command in the source tree, returning "" on any failure.
  """
  try:
    out = subprocess.run(
      ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True, timeout=5
    )
  except (OSError, subprocess.SubprocessError):
    return ""
  return out.stdout.strip()


def _commit():
  """
  Full HEAD commit. Falls back to GITHUB_SHA when building without a .git dir.
  """
  commit = _git("rev-parse", "HEAD") or os.environ.get("GITHUB_SHA", "")
  if not commit:
    return ""
  # Scoped to *.py: the LFS binaries under bin/ and backend/ routinely show as
  # modified after a smudge, which would mark every build dirty.
  if _git("status", "--porcelain", "--", "*.py"):
    commit += "-dirty"
  return commit


def _write_stamp(build_lib, version):
  """
  Overwrite mldebug/_build_info.py in the build tree with the version and commit.
  """
  commit = _commit()
  stamp = Path(build_lib) / "mldebug" / "_build_info.py"
  build_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
  stamp.write_text(
    '"""Generated at build time by setup.py."""\n\n'
    f'VERSION = "{version}"\n'
    f'COMMIT = "{commit}"\n'
    f'BUILD_DATE = "{build_date}"\n',
    encoding="utf-8",
  )
  print(f"[INFO] stamped {stamp} with commit '{commit}'")


class BuildPyStamped(build_py):
  """
  Stamp the build tree after the sources are copied, leaving the source tree untouched.
  """

  def run(self):
    super().run()
    _write_stamp(self.build_lib, self.distribution.get_version())


setup(cmdclass={"build_py": BuildPyStamped})
