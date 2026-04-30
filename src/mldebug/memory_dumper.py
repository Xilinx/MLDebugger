# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Memory dump operations for AIE debugging.

Handles L1, L2, L3 buffer dumps, output path management, and debug server lifecycle.
"""

import pathlib
import struct

from itertools import groupby

from mldebug.debug_server import DebugServer
from mldebug.utils import LOGGER


class MemoryDumper:
  """
  Handles all memory dump operations including L1, L2, and L3 buffers.

  Manages output directory creation, file writing (text/binary), and
  debug server initialization for L3 buffer coordination with FlexML runtime.
  """

  def __init__(self, args, output_dir, design_info, state, impl):
    """
    Args:
      args: Parsed command-line arguments.
      output_dir: Directory where debug outputs will be written.
      design_info: LayerInfo object with overlay and buffer metadata.
      state: DebugState tracking current layer, iteration, etc.
      impl: Default backend implementation (impls[0]) for memory reads.
    """
    self.args = args
    self.output_dir = output_dir
    self.design_info = design_info
    self.state = state
    self.impl = impl
    self.debug_server = None
    self._dir_cache = set()

  def get_output_path(self, buffer=None, col=None, row=None, layer_order=None, batch="0"):
    """
    Get or create the output path for storing memory dumps or debug files.

    Args:
      buffer: Buffer type or name.
      col: AIE column index.
      row: AIE row index.
      layer_order: Layer order number or custom value.
      batch: String for batch number (default "0").

    Returns:
      A string representing the full path to the output directory.
    """
    if self.args.l3:
      pathlib.Path(self.output_dir).mkdir(parents=True, exist_ok=True)
    if not layer_order:
      layer = self.state.get_current_layer()
      if layer:
        layer_order = layer.layer_order
      else:
        layer_order = "Unknown"
    p = f"{self.output_dir}/batch{batch}/layer_{layer_order}"
    if buffer:
      p += f"/{buffer}"
    if col is not None and row is not None:
      p += f"/{col}_{row}"
    if p not in self._dir_cache:
      pathlib.Path(p).mkdir(parents=True, exist_ok=True)
      self._dir_cache.add(p)
    return p

  def write_data_to_file(self, data, fname):
    """
    Write an array of data to file in text or binary format.

    Args:
      data: List of integer data (e.g., from buffer dumps).
      fname: Filename or path stub (extension added by function).
    """
    if self.args.run_flags.skip_dump:
      return

    if self.args.run_flags.text_dump:
      fname += ".txt"
      with open(fname, "w", encoding="utf-8") as file:
        formatted_data = "\n".join([f"{word:08x}" for word in data]) + "\n"
        file.write(formatted_data)
    else:
      fname += ".bin"
      with open(fname, "wb") as file:
        # Binary file requires 8 byte header that indicates size of data
        # Q: unsigned long long I: unsigned int
        binary_data = struct.pack("Q", len(data) * 4)
        binary_data += struct.pack("I" * len(data), *data)
        file.write(binary_data)

  def dump_memory_l2(self, buffers, it, layer_order=None, use_l2_names=False, sid=0):
    """
    Dump L2 memory buffers to file for a specific layer and iteration.

    Args:
      buffers: List of Buffer objects to dump.
      it: Iteration number.
      layer_order: Layer number for output directory.
      use_l2_names: Use named L2 outputs if True (for X2).
      sid: Stamp id for which the dump applies.
    """
    if self.args.run_flags.skip_dump:
      return

    overlay = self.design_info.overlay
    # batch + stamp combination doesn't exist
    if self.design_info.is_batched():
      batch = str(sid)
      suffix = "stamp0"
    else:
      batch = "0"
      suffix = f"stamp{sid}"

    for buffer in buffers:
      if buffer.ofm:
        it -= 1  # OFM is dumped at start of next iteration
      for buf_id, group in groupby(buffer.l2, key=lambda x: x.buf_id):
        buffer_name = f"l2_it{it}_{buf_id}_{suffix}"
        data = []
        for l2 in list(group):
          col = l2.col + sid * overlay.get_stampwidth()
          if (col, l2.row) not in overlay.get_tiles(self.args.aie_iface.MEM_TILE_T, stamp_id=sid):
            continue
          data.extend(self.impl.dump_memory(col, l2.row, l2.address, l2.size))
          if not data:
            break
          if use_l2_names and l2.name:
            buffer_name = l2.name
        if self.args.run_flags.l2_ifm_dump or self.args.run_flags.l2_dump_only:
          fname = f"{self.get_output_path(layer_order=layer_order, batch=batch)}/{buffer.type}_{buffer_name}"
        else:
          fname = f"{self.get_output_path(buffer=buffer.type, layer_order=layer_order, batch=batch)}/{buffer_name}"
        self.write_data_to_file(data, fname)

  def dump_memory_l1(self, buffers, it, is_ping=None, sid=0):
    """
    Dump L1 (AIE tile) memory buffers for a specific layer/iteration/stamp.

    Args:
      buffers: List of Buffer objects for L1 memory.
      it: Iteration number.
      is_ping: If specified, chooses ping or pong buffer (default: odd/even selector).
      sid: Stamp id (default 0).
    """
    if self.args.run_flags.skip_dump or self.args.run_flags.l2_dump_only:
      return

    batch = "0"
    if self.design_info.is_batched():
      batch = str(sid)

    for buffer in buffers:
      if not buffer.l1:
        continue
      if is_ping is None:
        is_ping = it % 2 == 1
      if is_ping:
        offset = buffer.l1.ping
        size = buffer.l1.ping_size
      else:
        offset = buffer.l1.pong
        size = buffer.l1.pong_size
      for c, r in self.design_info.overlay.get_tiles(self.args.aie_iface.AIE_TILE_T, stamp_id=sid):
        data = self.impl.dump_memory(c, r, offset, size)
        fname = f"{self.get_output_path(buffer.type, c, r, batch=batch)}/l1_{it}"
        self.write_data_to_file(data, fname)

  def dump_memory_all(self):
    """
    Dump all buffers for the current layer (L1, L2 inputs/weights/output).
    """
    if self.args.aie_only:
      print("This Functionality is disabled for aie-only debug.")
      return

    layer = self.state.get_current_layer()
    if not layer:
      print("Layer not found")
      return

    it = self.state.cur_it
    self.dump_memory_l2(layer.in_buffers + layer.wts_buffers + layer.out_buffers, it)
    self.dump_memory_l1(layer.in_buffers + layer.wts_buffers, it)
    self.dump_memory_l1(layer.out_buffers, it)
    print(f"[INFO] Memory dump complete at : {self.get_output_path()}")

  def dump_x2_buffers(self, layer, it):
    """
    Perform X2-specific buffer dumping: current layer L3 and previous layer's L2 OFM.

    Args:
      layer: Current Layer object.
      it: Current iteration number.
    """
    self.dump_l3_buffers(layer, x2=True)
    previous_layer = self.state.get_previous_layer()
    if previous_layer:
      self.dump_memory_l2(previous_layer.out_buffers, it, previous_layer.layer_order, use_l2_names=True)

  def dump_l3_buffers(self, layer, x2=False):
    """
    Send requests to fetch L3 buffers for the current layer using the debug server.

    Args:
      layer: Layer object whose L3/Tensor buffers should be dumped.
      x2: If True, use the X2 tensor name convention for buffer naming.
    """
    self.get_output_path()
    if not self.state.error and self.debug_server:
      for buffer in layer.l3_buffers:
        name = buffer.tensor_name if x2 else buffer.name
        self.debug_server.send_request(name, buffer.offset, buffer.size)

  def _ensure_debug_server(self):
    """
    Initialize debug server if not already active.

    Returns:
      True if debug server is ready for use, False otherwise.
    """
    if not self.debug_server:
      LOGGER.log("[INFO] Starting L3 debug server...")
      self.debug_server = DebugServer(None, self.output_dir, self.args.backend == "test")
      if not self.debug_server.client_socket and self.args.backend != "test":
        LOGGER.log(
          "[ERROR] Failed to connect to FlexML runtime. Make sure FlexML is running and waiting for debugger connection."
        )
        return False
    if not self.debug_server.client_socket and self.args.backend != "test":
      LOGGER.log("[ERROR] No active connection to FlexML runtime. L3 dump failed.")
      return False
    return True

  def dump_l3_buffers_manual(self, name, offset, size):
    """
    Manually dump a specified L3 buffer in AIE-only mode.

    Args:
      name: Name of the L3 buffer.
      offset: Offset for the L3 buffer.
      size: Size (in bytes) of the L3 buffer to dump.
    """
    if not self._ensure_debug_server():
      return

    success = self.debug_server.send_request(name, offset, size, current_dir=True)
    if success:
      LOGGER.log(f"[INFO] L3 buffer '{name}' dumped successfully (offset={offset}, size={size})")
    else:
      LOGGER.log(f"[ERROR] Failed to dump L3 buffer '{name}'")

  def dump_l3_buffers_interactive(self):
    """
    Dump all L3 buffers of the current layer interactively.
    """
    if not self._ensure_debug_server():
      return

    self.dump_l3_buffers(self.state.get_current_layer(), x2=self.args.x2_folder_path is not None)
    if self.state.get_current_layer() and self.state.get_current_layer().l3_buffers:
      for buffer in self.state.get_current_layer().l3_buffers:
        LOGGER.log(f"[INFO] L3 buffer '{buffer.name}' dumped successfully (offset={buffer.offset}, size={buffer.size})")
      LOGGER.log(f"[INFO] Memory dump complete at : {self.get_output_path()}")
