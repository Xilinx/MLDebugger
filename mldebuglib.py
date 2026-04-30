# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
This module exposes mldebugger functionality to external python clients
"""

import os
import importlib

from mldebug.aie_status import AIEStatus as _AIES
from mldebug.arch import AIE_DEV_PHX, AIE_DEV_STX, AIE_DEV_TEL
from mldebug.arch import load_aie_arch as _load_aie
from mldebug.aie_overlay import Overlay as _OL
from mldebug.input_parser import check_hw_context as _check_hwc
from mldebug.input_parser import check_registry_keys as _check_reg


class MLDebug:
  """
  Top Level MLDebugger class. All arguments are optional.
  Device (predefined): AIE_DEV_PHX
                       AIE_DEV_STX
                       AIE_DEV_TEL
  Overlay(str): Overlay in the form: cxr
  ctxid(int): context id of the xrt process. If not passed, it is detected automatically
  pid(int): pid of the xrt process. If not passed, it is detected automatically
  Note: Only XRT Backend is supported
  """

  # Parse overlay string to create layout array [stamps, ncol, nrow]
  @staticmethod
  def parse_overlay_string(overlay_str):
      """
      Parse overlay string and convert to layout array.
      Examples:
        "4x4" -> [1, 4, 4]     (default 1 stamp)
        "2x4x4" -> [2, 4, 4]   (2 stamps, 4 cols, 4 rows)
        "1x8x8" -> [1, 8, 8]   (1 stamp, 8 cols, 8 rows)
      """
      parts = overlay_str.split('x')

      if len(parts) == 2:
          # Format: "4x4" (cols x rows, default to 1 stamp)
          cols, rows = map(int, parts)
          return [1, cols, rows]
      elif len(parts) == 3:
          # Format: "2x4x4" (stamps x cols x rows)
          stamps, cols, rows = map(int, parts)
          return [stamps, cols, rows]
      else:
          # Fallback to default if format is unexpected
          print(f"Warning: Unexpected overlay format '{overlay_str}', using default [1, 4, 4]")
          return [1, 4, 4]

  def __init__(self, device=AIE_DEV_STX, overlay="4x4", ctxid=None, pid=None):
    if os.name == "nt":
      _check_reg()
    if ctxid is None or pid is None:
      ctxid, pid = _check_hwc(device)  # Fixed: pass device parameter to check_hw_context
    # Initialize Debug
    try:
      xrt_impl = importlib.import_module("mldebug.backend.xrt_impl")
    except ModuleNotFoundError as e:
      raise RuntimeError("Unable to import XRT Backend. Please check if python version is 3.10.") from e
    except ImportError as e:
      raise RuntimeError("Unable to import XRT Backend. Please check XRT Installation.") from e
    self.aie_iface = _load_aie(device)

    # Create a proper args-like object for Overlay constructor
    class OverlayArgs:
      def __init__(self, aie_iface, overlay_string):
        self.aie_iface = aie_iface
        self.overlay = overlay_string

    overlay_args = OverlayArgs(self.aie_iface, overlay)

    layout = self.parse_overlay_string(overlay)
    self._ov_hdl = _OL(overlay_args, layout)

    tiles = self._ov_hdl.get_tiles(self.aie_iface.AIE_TILE_T, stamp_id=0)
    self._be = xrt_impl.XRTImpl(tiles, ctxid, pid, device)
    self._st_hdl = _AIES(self._be, self._ov_hdl.get_tiles, self.aie_iface, overlay)

    # Expose be functionality
    self.read_register = self._be.read_register
    self.write_register = self._be.write_register
    self.print_register = self._be.print_register
    self.read_memory = self._be.dump_memory

  def print_aie_status(self, filename=None, tile_type=None, vaiml=False, advanced=False):
    """
    Print AIE status
    filename (str): write status to specified file
    tile_type (list): list of tile types to include in status. Default: all types
      Valid tile_types: MLDebug.aie_iface.AIE_TILE_T
                        MLDebug.aie_iface.SHIM_TILE_T
                        MLDebug.aie_iface.MEM_TILE_T
    vaiml (bool): add vaiml specific metadata to status
    advanced (bool): Add advanced info like bds, locks
    """
    self._st_hdl.get(filename, tile_type, vaiml, advanced)

  def get_aie_status(self, tile_type=None, vaiml=False, advanced=False):
    """
    Return AIE status raw data structure
    filename (str): write status to specified file
    tile_type (list): list of tile types to include in status. Default: all types
      Valid tile_types: MLDebug.aie_iface.AIE_TILE_T
                        MLDebug.aie_iface.SHIM_TILE_T
                        MLDebug.aie_iface.MEM_TILE_T
    vaiml (bool): add vaiml specific metadata to status
    advanced (bool): Add advanced info like bds, locks
    """
    self._st_hdl.update(tile_type=tile_type, vaiml=vaiml, advanced=advanced)
    return self._st_hdl.results
