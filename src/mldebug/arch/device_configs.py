# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2026 Advanced Micro Devices, Inc. All rights reserved.

"""
Central device geometry registry -- single source of truth for per-device
and per-variant AIE geometry.

Keyed by device/variant name. Fields:
  arch      -- which defs module the loader should import (aie2p/aie2ps)
  base      -- base device name understood by the C++ binding and xrt-smi;
               variants (e.g. t20) map back to their base (telluride)
  hwGen     -- hardware generation (matches the core-dump header hwGen)
  core_row_start -- first core-tile row; also the AIE tile row offset
  numrows/numcols -- full-device geometry, used to disambiguate variants
                     that share a hwGen

Variants may share a hwGen (telluride/t20) and are disambiguated by
(numrows, numcols).
"""

MB = 1024 * 1024

# Device / variant name constants. Defined here (the lowest-level arch module)
# so they can key DEVICE_CONFIGS; re-exported by loader for the rest of the code.
AIE_DEV_PHX = "phx"
AIE_DEV_STX = "stx"
AIE_DEV_TEL = "telluride"
AIE_DEV_NPU3 = "npu3"
AIE_DEV_T20 = "t20"
AIE_DEV_T10 = "t10"

# Arch (defs-module) names, resolved to modules by the loader.
ARCH_AIE2P = "aie2p"
ARCH_AIE2PS = "aie2ps"

DEVICE_CONFIGS = {
  AIE_DEV_PHX: {
    "arch": ARCH_AIE2P,
    "base": AIE_DEV_PHX,
    "hwGen": 2,
    "baseAddr": 0x0,
    "core_row_start": 2,
    "mem_row_start": 1,
    "memtile_rows": 1,
    "numrows": 6,
    "numcols": 4,
    "shim_tile_block_size": MB,
    "mem_tile_block_size": MB,
    "core_tile_block_size": MB,
    "mem_tile_sz": 0x80000,
  },
  AIE_DEV_STX: {
    "arch": ARCH_AIE2P,
    "base": AIE_DEV_STX,
    "hwGen": 4,
    "baseAddr": 0x0,
    "core_row_start": 2,
    "mem_row_start": 1,
    "memtile_rows": 1,
    "numrows": 6,
    "numcols": 8,
    "shim_tile_block_size": MB,
    "mem_tile_block_size": MB,
    "core_tile_block_size": MB,
    "mem_tile_sz": 0x80000,
  },
  AIE_DEV_TEL: {
    "arch": ARCH_AIE2PS,
    "base": AIE_DEV_TEL,
    "hwGen": 5,
    "baseAddr": 0x0,
    "core_row_start": 3,
    "mem_row_start": 1,
    "memtile_rows": 2,
    "numrows": 7,
    "numcols": 36,
    "shim_tile_block_size": MB,
    "mem_tile_block_size": MB,
    "core_tile_block_size": MB,
    "mem_tile_sz": 0x80000,
  },
  # aie2ps-based variants sharing telluride's hwGen; disambiguated by geometry.
  AIE_DEV_T20: {
    "arch": ARCH_AIE2PS,
    "base": AIE_DEV_TEL,
    "hwGen": 5,
    "baseAddr": 0x0,
    "core_row_start": 2,
    "mem_row_start": 1,
    "memtile_rows": 1,
    "numrows": 6,
    "numcols": 24,
    "shim_tile_block_size": MB,
    "mem_tile_block_size": MB,
    "core_tile_block_size": MB,
    "mem_tile_sz": 0x80000,
  },
  AIE_DEV_T10: {
    "arch": ARCH_AIE2PS,
    "base": AIE_DEV_TEL,
    "hwGen": 5,
    "baseAddr": 0x0,
    "core_row_start": 2,
    "mem_row_start": 1,
    "memtile_rows": 1,
    "numrows": 4,
    "numcols": 8,
    "shim_tile_block_size": MB,
    "mem_tile_block_size": MB,
    "core_tile_block_size": MB,
    "mem_tile_sz": 0x80000,
  },
}


def get_base_device(name):
  """
  Return the base device name for a device/variant. Unknown names map to
  themselves so callers can pass through user-specified devices unchanged.
  """
  cfg = DEVICE_CONFIGS.get(name)
  return cfg["base"] if cfg else name


def resolve_variant(hw_gen, num_rows=None, num_cols=None):
  """
  Resolve a device/variant name from a hardware generation and (optional)
  full-device geometry.

  Matches hwGen first. When several variants share a hwGen, disambiguate by
  (num_rows, num_cols). Falls back to the first (base) candidate when the
  geometry is unavailable or does not match a variant. Returns None if no
  device matches the hwGen.
  """
  candidates = [n for n, c in DEVICE_CONFIGS.items() if c["hwGen"] == hw_gen]
  if not candidates:
    return None
  if len(candidates) == 1:
    return candidates[0]
  if num_rows is not None and num_cols is not None:
    for n in candidates:
      c = DEVICE_CONFIGS[n]
      if c["numrows"] == num_rows and c["numcols"] == num_cols:
        return n
  # Base device is listed first among same-hwGen candidates.
  return candidates[0]
