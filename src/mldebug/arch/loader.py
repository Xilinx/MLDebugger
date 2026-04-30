# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Load appropriate module based on device
"""

import importlib

AIE_DEV_PHX = "phx"
AIE_DEV_STX = "stx"
AIE_DEV_TEL = "telluride"
AIE_DEV_NPU3 = "npu3"

def load_aie_arch(device):
  """
  return specific aie arch module based on name
  """
  mod = ".aie2p_defs"
  if device == AIE_DEV_TEL:
    mod = ".aie2ps_defs"
  elif device == AIE_DEV_NPU3:
    mod = ".npu3_defs"
  return importlib.import_module(mod, package="mldebug.arch")
