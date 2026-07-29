# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Load appropriate module based on device
"""

import importlib

from .device_configs import AIE_DEV_NPU3, ARCH_AIE2P, ARCH_AIE2PS, DEVICE_CONFIGS

# Maps a config's `arch` field to its defs module.
_ARCH_MODULES = {
  ARCH_AIE2P: ".aie2p_defs",
  ARCH_AIE2PS: ".aie2ps_defs",
}


def load_aie_arch(device):
  """
  return specific aie arch module based on device/variant name
  """
  # npu3 is not yet in the geometry registry; keep it as a special case.
  if device == AIE_DEV_NPU3:
    return importlib.import_module(".npu3_defs", package="mldebug.arch")

  cfg = DEVICE_CONFIGS.get(device)
  arch = cfg["arch"] if cfg else ARCH_AIE2P
  mod = _ARCH_MODULES.get(arch, ".aie2p_defs")
  return importlib.import_module(mod, package="mldebug.arch")
