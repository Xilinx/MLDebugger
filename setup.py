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
from setuptools.command.sdist import sdist

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
  # Scoped to tracked *.py: the LFS binaries under bin/ and backend/ show as modified
  # after a smudge, and sdist leaves an untracked copy of the tree behind while it runs.
  if _git("status", "--porcelain", "-uno", "--", "*.py"):
    commit += "-dirty"
  return commit


def _write_stamp(stamp, version):
  """
  Write _build_info.py, or keep the existing one when the commit is unknown.
  An unknown commit means we are building from an sdist, which already carries
  the stamp written when the sdist itself was built.
  """
  commit = _commit()
  if not commit:
    print(f"[WARNING] no git commit found, leaving {stamp} as is")
    return
  build_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
  # sdist hard links files out of the source tree, so replace instead of writing in place.
  stamp.unlink(missing_ok=True)
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
    stamp = Path(self.build_lib) / "mldebug" / "_build_info.py"
    _write_stamp(stamp, self.distribution.get_version())


class SdistStamped(sdist):
  """
  Stamp the sdist so a wheel built from it keeps the commit.
  """

  def make_release_tree(self, base_dir, files):
    super().make_release_tree(base_dir, files)
    stamp = Path(base_dir) / "src" / "mldebug" / "_build_info.py"
    _write_stamp(stamp, self.distribution.get_version())


setup(cmdclass={"build_py": BuildPyStamped, "sdist": SdistStamped})
