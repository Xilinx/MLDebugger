# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Backend factory for creating backend implementation instances.

Centralizes backend selection, lazy imports, and error handling so that
callers only need to build a BackendConfig and call create_backend().
"""

import importlib
import sys

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BackendConfig:
  """
  Superset of parameters needed by all backend constructors.

  Each backend extracts only the fields it needs:
    XRT:       tiles, ctx_id, pid, device
    Test:      tiles, design_info, args
    CoreDump:  tiles, ctx_id, pid, device, core_dump_file, no_header
  """

  tiles: list = field(default_factory=list)
  ctx_id: int = 0
  pid: int = 0
  device: str = ""
  design_info: Any = None
  args: Any = None
  core_dump_file: str = None
  no_header: bool = False


def create_backend(backend_type, config):
  """
  Create and return a backend implementation instance.

  Handles lazy imports for backends with optional dependencies (XRT)
  and provides clear error messages when dependencies are missing.

  Args:
    backend_type: One of "xrt", "test", or "core_dump".
    config: BackendConfig with the parameters for the backend.

  Returns:
    A BackendInterface implementation instance.
  """
  if backend_type == "xrt":
    try:
      xrt_mod = importlib.import_module("mldebug.backend.xrt_impl")
    except ModuleNotFoundError:
      print("Unable to import Backend. Python 3.10 is required on Win/Linux and 3.12 on Embedded Linux.")
      sys.exit(1)
    except ImportError:
      print("Unable to import XRT. Please check install.")
      sys.exit(1)
    return xrt_mod.XRTImpl(config.tiles, config.ctx_id, config.pid, config.device)

  if backend_type == "test":
    test_mod = importlib.import_module("mldebug.backend.test_impl")
    return test_mod.TestImpl(config.tiles, config.design_info, config.args)

  # core_dump (default)
  core_dump_mod = importlib.import_module("mldebug.backend.core_dump_impl")
  return core_dump_mod.CoreDumpImpl(
    config.tiles, config.ctx_id, config.pid, config.device,
    core_dump_file=config.core_dump_file, no_header=config.no_header
  )
