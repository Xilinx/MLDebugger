# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Top level AIE Arch Module
"""

from .device_configs import (
  AIE_DEV_NPU3,
  AIE_DEV_PHX,
  AIE_DEV_STX,
  AIE_DEV_T10,
  AIE_DEV_T50,
  AIE_DEV_TEL,
  DEVICE_CONFIGS,
  get_base_device,
  resolve_variant,
)
from .loader import load_aie_arch

__all__ = [
  "AIE_DEV_NPU3",
  "AIE_DEV_PHX",
  "AIE_DEV_STX",
  "AIE_DEV_T10",
  "AIE_DEV_T50",
  "AIE_DEV_TEL",
  "DEVICE_CONFIGS",
  "get_base_device",
  "load_aie_arch",
  "resolve_variant",
]
