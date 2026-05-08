#!/usr/bin/env python3
"""Post-process a built mldebug_xdp wheel into a linux_aarch64 wheel.

Takes the standard ``py3-none-any`` wheel produced by ``uv build``, removes
the Windows and x86 Linux binaries that are not usable on aarch64, and
re-tags the wheel as ``py3-none-linux_aarch64``.

Usage:
    python scripts/build_aarch64_wheel.py dist/mldebug_xdp-0.1.0-py3-none-any.whl
    python scripts/build_aarch64_wheel.py dist/  # picks the first .whl in dir
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Files inside the wheel (relative to wheel root) that are NOT needed on aarch64.
EXCLUDE_FILES = [
  "mldebug/bin/c++filt.exe",
  "mldebug/bin/llvm-objdump.elf",
  "mldebug/bin/llvm-objdump.exe",
  "mldebug/backend/xrt_backend.cp310-win_amd64.pyd",
  "mldebug/backend/xrt_backend.cpython-310-x86_64-linux-gnu.so",
]

PLATFORM_TAG = "linux_aarch64"
PYTHON_TAG = "py3"
ABI_TAG = "none"


def resolve_input_wheel(arg: Path) -> Path:
  if arg.is_dir():
    wheels = sorted(arg.glob("*.whl"))
    if not wheels:
      sys.exit(f"no .whl found in {arg}")
    return wheels[0]
  if not arg.is_file():
    sys.exit(f"wheel not found: {arg}")
  return arg


def rewrite_wheel_metadata(dist_info: Path) -> None:
  wheel_meta = dist_info / "WHEEL"
  lines = wheel_meta.read_text().splitlines()
  out: list[str] = []
  saw_root = False
  saw_tag = False
  for line in lines:
    if line.startswith("Tag:"):
      if not saw_tag:
        out.append(f"Tag: {PYTHON_TAG}-{ABI_TAG}-{PLATFORM_TAG}")
        saw_tag = True
      # drop any additional Tag lines from the original wheel
      continue
    if line.startswith("Root-Is-Purelib:"):
      out.append("Root-Is-Purelib: false")
      saw_root = True
      continue
    out.append(line)
  if not saw_root:
    out.append("Root-Is-Purelib: false")
  wheel_meta.write_text("\n".join(out) + "\n")


def main() -> None:
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("wheel", type=Path, help="Path to source wheel or dist directory")
  ap.add_argument(
    "--out-dir",
    type=Path,
    default=Path("dist"),
    help="Directory to write the aarch64 wheel into (default: dist)",
  )
  args = ap.parse_args()

  src_wheel = resolve_input_wheel(args.wheel)
  out_dir = args.out_dir.resolve()
  out_dir.mkdir(parents=True, exist_ok=True)

  work = out_dir / "_aarch64_unpack"
  if work.exists():
    shutil.rmtree(work)
  work.mkdir()

  print(f"unpacking {src_wheel.name}")
  subprocess.check_call(
    [sys.executable, "-m", "wheel", "unpack", str(src_wheel), "--dest", str(work)],
  )

  unpacked_dirs = [p for p in work.iterdir() if p.is_dir()]
  if len(unpacked_dirs) != 1:
    sys.exit(f"expected one unpacked dir under {work}, got {unpacked_dirs}")
  unpacked = unpacked_dirs[0]

  for rel in EXCLUDE_FILES:
    target = unpacked / rel
    if target.exists():
      target.unlink()
      print(f"removed {rel}")
    else:
      print(f"warning: not found in wheel, skipping: {rel}", file=sys.stderr)

  dist_info_dirs = list(unpacked.glob("*.dist-info"))
  if len(dist_info_dirs) != 1:
    sys.exit(f"expected one *.dist-info under {unpacked}, got {dist_info_dirs}")
  rewrite_wheel_metadata(dist_info_dirs[0])

  # Re-pack. ``wheel pack`` regenerates RECORD and uses the WHEEL Tag
  # entry to pick the output filename, so the result is automatically
  # named mldebug_xdp-<ver>-py3-none-linux_aarch64.whl.
  print("repacking with aarch64 platform tag")
  subprocess.check_call(
    [sys.executable, "-m", "wheel", "pack", str(unpacked), "--dest-dir", str(out_dir)],
  )

  shutil.rmtree(work)

  produced = sorted(out_dir.glob(f"*-{PLATFORM_TAG}.whl"))
  if not produced:
    sys.exit("aarch64 wheel was not produced")
  print(f"wrote {produced[-1]}")


if __name__ == "__main__":
  main()
