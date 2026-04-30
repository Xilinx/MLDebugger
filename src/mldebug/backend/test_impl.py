# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Test Backend
"""

import random

from mldebug.utils import print_tile_grid
from .backend_interface import BackendInterface

class TestImpl(BackendInterface):
  """
  Test Backend Top Class
  """

  is_simulation = True

  def __init__(self, aie_debug_tiles, buffer_info, args) -> None:
    """
    Initialize the TestImpl backend.

    Args:
      aie_debug_tiles (list): List of tiles under AIE debug control.
      buffer_info (object): Parsed buffer information containing layers.
      args (object): Command-line or config arguments, must have 'aie_only', 'run_flags.mock_hang'.
    """
    self.aie_debug_tiles = aie_debug_tiles
    self.start_pc = 0
    self.end_pc = 0
    self.layers = buffer_info.layers
    self.current_layer = -1
    self.depth_itr_cnt = 0
    self.hang_state = False
    self.hang_layer = -1
    self.aie_only = args.aie_only
    self.pc_brkpts = [0, 0]
    self.simulate_hang = args.run_flags.mock_hang and not self.aie_only
    if self.simulate_hang:
      self.hang_layer = random.randrange(0, len(self.layers))

  def poll_core_status(self):
    """
    Polls the core debug status.

    Returns:
      int: 1 if core is halted (OK), 0 if simulating hang state (not halted).
    """
    if self.hang_state:
      return 0
    return 1

  def set_performance_counter_halt(self):
    """
    Set performance counter halt event.

    Args:
      None

    Returns:
      None
    """
    print("Setting Performance Counter Halt.")

  def read_core_pc(self, all_tiles=False):
    """
    Reads the current (simulated) core Program Counter value.

    Args:
      all_tiles (bool, optional): If True, returns value for all debug tiles. Default is False.

    Returns:
      int or list[int]: PC value or list of PC values for all tiles.
    """
    pc = self.start_pc
    if not self.aie_only:
      if self.hang_state:
        return 1001
      if self.depth_itr_cnt == self.layers[self.current_layer].lcp.depth_iter and self.end_pc:
        self.depth_itr_cnt = 0
        pc = self.end_pc
      else:
        self.depth_itr_cnt += 1
    if all_tiles:
      return [pc] * len(self.aie_debug_tiles)
    return pc

  def continue_aie(self):
    """
    Un-halts the AIE and resumes execution.

    Args:
      None

    Returns:
      None
    """
    # print("Continuing AIE Execution")

  def single_step(self, num_instr=1):
    """
    Single step all AIE cores.

    Args:
      num_instr (int, optional): Number of instructions to step. Default is 1.

    Returns:
      None
    """
    print(f"Single Step {num_instr} Instructions")

  def _switch_layer(self):
    """
    Advance internal pointer to the next layer and reset depth iteration.

    Args:
      None

    Returns:
      None
    """
    self.current_layer += 1
    self.depth_itr_cnt = 0
    if self.current_layer == self.hang_layer:
      layer = self.layers[self.hang_layer]
      print(f"Simulating Hang at: Layer {layer.layer_order} : {layer.stamps[0].name}")
      self.hang_state = True

  def set_pc_breakpoint(self, pc_value, idx=0):
    """
    Enables creation of a valid Program Counter event when
    the given instruction line number is reached.

    Args:
      pc_value (int): PC value at which to set breakpoint.
      idx (int, optional): Breakpoint event index; 0 for start, 1 for end. Default is 0.

    Returns:
      None

    Raises:
      ValueError: If idx is not 0 or 1.
    """
    if self.aie_only:
      self.start_pc = pc_value
      return
    if idx == 0:
      self._switch_layer()
      self.start_pc = pc_value
    elif idx == 1:
      self.end_pc = pc_value
    else:
      raise ValueError(f"PC ID {idx} unsupported")
    # print("Set PC Breakpoint: ", pc_value)

  def clear_pc_breakpoint(self, idx=0):
    """
    Clear the specified PC breakpoint event.

    Args:
      idx (int, optional): Breakpoint event index; 0 for start, 1 for end. Default is 0.

    Returns:
      None
    """
    if idx == 0:
      self.start_pc = 0
    else:
      self.end_pc = 0

  def enable_pc_halt(self):
    """
    Set a breakpoint for PC halt event (simulated).

    Args:
      None

    Returns:
      None
    """
    # print("Enabling PC Halt")

  def get_pc(self):
    """
    Reads the current PC Value from a tile.

    Args:
      None

    Returns:
      int: PC value.
    """
    return self.read_core_pc()

  def dump_memory(self, c, r, buffer_offset, buffer_size, filename=None):
    """
    Dumps L1/L2 memory (simulated).

    Args:
      c (int): Tile column (ignored in test backend).
      r (int): Tile row (ignored in test backend).
      buffer_offset (int): Memory address (ignored in test backend).
      buffer_size (int): Size of the buffer to read in bytes.

    Returns:
      list[int]: List of dummy values.
    """
    buffer_size = min(buffer_size, 400)
    return [0xDEADBEEF] * int((buffer_size / 4))

  def read_register(self, c, r, reg):
    """
    Return a dummy register value.

    Args:
      *_: Ignored.

    Returns:
      int: Dummy register value (0xDEADBEEF).
    """
    return 0xDEADBEEF

  def print_register(self, c, r, reg):
    """
    Print dummy register value in decimal, binary and hex.

    Args:
      *_: Ignored.

    Returns:
      None
    """
    value = 0xDEADBEEF
    print(f"Decimal: {value}")
    print(f"Binary:  {bin(value)[2:]:>08}")
    print(f"Hex:     0x{hex(value)[2:].upper():>02}")

  def write_register(self, c, r, reg, value):
    """
    Dummy stub for writing to a register.

    Args:
      *_: Ignored.

    Returns:
      None
    """
    return

  def read_performance_counters(self, *_):
    """
    Print dummy performance counter value.

    Args:
      *_: Ignored.

    Returns:
      None
    """
    print("0xdeadbeef")

  def read_core_debug_status(self):
    """
    Prints the simulated core debug status for all debug tiles.

    Args:
      None

    Returns:
      None
    """
    print_tile_grid("Core Debug Status", self.aie_debug_tiles, register_values=[0xDEAD] * len(self.aie_debug_tiles))

  def read_core_execution_status(self):
    """
    Prints the simulated core execution status for all debug tiles.

    Args:
      None

    Returns:
      None
    """
    print_tile_grid("Core Execution Status", self.aie_debug_tiles, register_values=[0xDEAD] * len(self.aie_debug_tiles))

  def read_all_core_pc(self):
    """
    Reads and prints the Program Counter for all tiles.

    Args:
      None

    Returns:
      None
    """
    pc = self.read_core_pc(all_tiles=True)
    print_tile_grid("Core PC", self.aie_debug_tiles, register_values=pc, format_type="int")

  def print_pc_breakpoints(self):
    """
    Print currently configured PC events.

    Args:
      None

    Returns:
      None
    """
    print(f"Currently configured PC Breakpoints: {self.pc_brkpts}")

  def write_aie_regs(self, reg, value):
    """
    Dummy stub for writing to AIE registers.

    Args:
      *_: Ignored.

    Returns:
      None
    """
    return

  def read_aie_regs(self, reg):
    """
    Reads a register in all of debug aie cores (simulated).

    Args:
      reg (int): Register identifier (ignored in test backend).

    Returns:
      list[int]: List of dummy register values for each debug tile.
    """
    return [0xDEADBEEF] * len(self.aie_debug_tiles)

  def configure_performance_counters(self):
    """
    Configure performance counters (dummy implementation for testing)
    Args:
      None
    """
    print("Configuring Performance Counters")

  def disable_pc_halt(self):
    """
    Disable PC halt
    Args:
      None
    """
    return