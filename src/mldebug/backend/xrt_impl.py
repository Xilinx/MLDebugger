# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
XRT Backend
"""

from mldebug.arch import AIE_DEV_TEL
from mldebug.utils import print_tile_grid
from .xrt_backend import MlDebug
from .backend_interface import BackendInterface


class XRTImpl(BackendInterface):
  """
  XRT Backend top
  """

  def __init__(self, aie_overlay_tiles, ctx_id, pid, dev_name, debug_library=False, core_dump_file=None) -> None:
    """
    Initialize the XRTImpl backend

    Args:
      aie_overlay_tiles (list[tuple[int, int]]): A list of (col, row) tuples for overlay AIE core tiles.
      ctx_id (int): Context ID for the runtime.
      pid (int): Process ID for the job context.
      dev_name (str): Device name string.
      debug_library (bool, optional): Whether to use the debug library backend.
      core_dump_file (str, optional): Path to the core dump file.
    """
    self.overlay_aie_core_tiles = aie_overlay_tiles
    use_debug_library = "debuglibrary" if debug_library else "xrt"
    if dev_name != AIE_DEV_TEL:
      self.binding = MlDebug(list(self.overlay_aie_core_tiles), ctx_id, pid, dev_name, use_debug_library)
    else:
      self.binding = MlDebug(list(self.overlay_aie_core_tiles), ctx_id, pid, dev_name)
    self.pc_brkpts = [0, 0]

  def read_core_debug_status(self):
    """
    Reads the core debug status and prints it as a tile grid.

    Args:
      None

    Returns:
      None
    """
    status = self.binding.read_core_debug_status()
    print_tile_grid("Core Debug Status", self.overlay_aie_core_tiles, status)

  def read_core_execution_status(self):
    """
    Reads the core execution status and prints it as a tile grid.

    Args:
      None

    Returns:
      None
    """
    status = self.binding.read_core_execution_status()
    print_tile_grid("Core Execution Status", self.overlay_aie_core_tiles, status)

  def poll_core_status(self):
    """
    Polls the core debug status.

    Args:
      None

    Returns:
      int: 1 if core is halted, 0 otherwise
    """
    return self.binding.poll_core_status()

  def configure_performance_counters(self):
    """
    Configure performance counter registers (example for tile (2, 2)).

    Args:
      None

    Returns:
      None
    """
    disabled_event = 29
    rising_edge_event = 13
    rising_edge_control = disabled_event + (1 << 9)
    print(rising_edge_control)
    perf_control = rising_edge_event + (rising_edge_event << 8)
    for c, r in [(2, 2)]:
      self.binding.write_register(c, r, 0x00034408, rising_edge_control)
      self.binding.write_register(c, r, 0x00031500, perf_control)
    print()

  def set_performance_counter_halt(self):
    """
    Set the performance counter halt.

    Args:
      None

    Returns:
      None
    """
    self.binding.set_performance_counter_halt()

  def read_core_pc(self, all_tiles=False):
    """
    Reads the current core Program Counter line.

    Args:
      all_tiles (bool, optional): If True, read PC for all tiles, else just the first.

    Returns:
      int or list[int]: PC value(s)
    """
    if all_tiles:
      return self.binding.read_core_pc()
    return self.binding.read_core_pc()[0]

  def read_all_core_pc(self):
    """
    Reads and prints the Program Counter for all tiles.

    Args:
      None

    Returns:
      None
    """
    pc = self.read_core_pc(all_tiles=True)
    print_tile_grid("Core PC", self.overlay_aie_core_tiles, register_values=pc, format_type="int")

  def continue_aie(self):
    """
    Un-halts the AIE and resumes execution.

    Args:
      None

    Returns:
      None
    """
    self.binding.continue_aie()

  def set_pc_breakpoint(self, pc_value, idx=0):
    """
    Enable a Program Counter event when a given instruction line number is reached.

    Args:
      pc_value (int): The PC value at which to set the breakpoint.
      idx (int, optional): Index of event [0 or 1]. Default is 0.

    Returns:
      None
    """
    if idx not in (0, 1):
      print("Only PC Event 0 and 1 are supported for breakpoint")
      return
    self.pc_brkpts[idx] = pc_value
    self.binding.set_pc_breakpoint(pc_value, idx)

  def clear_pc_breakpoint(self, idx=0):
    """
    Clear a PC breakpoint event.

    Args:
      idx (int, optional): Index of event [0 or 1]. Default is 0.

    Returns:
      None
    """
    if idx not in (0, 1):
      return
    self.pc_brkpts[idx] = 0
    self.binding.set_pc_breakpoint(0, idx)

  def print_pc_breakpoints(self):
    """
    Print currently configured PC events.

    Args:
      None

    Returns:
      None
    """
    print(f"Currently configured PC Breakpoints: {self.pc_brkpts}")

  def enable_pc_halt(self):
    """
    Sets a breakpoint that instructs the AIE to halt when the PC event is hit.

    Args:
      None

    Returns:
      None
    """
    self.binding.enable_program_counter_halt()

  def get_pc(self):
    """
    Reads the current PC Value from a tile.

    Args:
      None

    Returns:
      int: The Program Counter value of a tile.
    """
    return self.binding.get_pc()

  def disable_pc_halt(self):
    """
    Disables the Program Counter halt breakpoint.

    Args:
      None

    Returns:
      None
    """
    self.binding.disable_pc_halt()

  def dump_memory(self, c, r, buffer_offset, buffer_size, filename=None):
    """
    Read and optionally dump L1/L2 tile memory.

    Args:
      c (int): Column of the tile.
      r (int): Row of the tile.
      buffer_offset (int): Memory offset to begin reading.
      buffer_size (int): Number of bytes to read.
      filename (str, optional): If given, dump values to file.

    Returns:
      list[int] or None: Word list if filename is not provided, else None.
    """
    chunk_sz = 0x1000
    chunks = []
    num_chunks = buffer_size // chunk_sz
    remainder = buffer_size % chunk_sz

    for i in range(num_chunks):
      offset = buffer_offset + i * chunk_sz
      chunk = self.binding.dump_buffer(c, r, offset, chunk_sz)
      chunks.extend(chunk)

    if remainder > 0:
      offset = buffer_offset + num_chunks * chunk_sz
      chunk = self.binding.dump_buffer(c, r, offset, remainder)
      chunks.extend(chunk)

    if filename:
      with open(filename, "w", newline="\n", encoding="utf-8") as file:
        for word in chunks:
          file.write(f"{word:08x}\n")
      print("Memory dumped to file: ", filename)
      return

    return chunks

  def read_register(self, c, r, reg):
    """
    Reads a register from a tile.

    Args:
      c (int): Column of the tile.
      r (int): Row of the tile.
      reg (int): Register offset.

    Returns:
      int: Register value.
    """
    return self.binding.read_register(c, r, reg)

  def print_register(self, c, r, reg):
    """
    Reads and displays a tile register in multiple formats for humans.

    Args:
      c (int): Column of the tile.
      r (int): Row of the tile.
      reg (int): Register offset.

    Returns:
      None
    """
    value = self.binding.read_register(c, r, reg)
    print(f"Hex:     0x{hex(value)[2:].upper():>02}")
    print(f"Decimal: {value}")
    binary = f"{value & 0xFFFFFFFF:032b}"
    # Insert spaces every 4 bits
    spaced_binary = " ".join(binary[i : i + 4] for i in range(0, 32, 4))
    # Create bit position indicators
    pos_tens = "3322 2222 2222 1111 1111 11"
    pos_ones = "1098 7654 3210 9876 5432 1098 7654 3210"
    underline = "-" * len(spaced_binary)

    print(f"Binary: \n{pos_tens}\n{pos_ones}\n{underline}\n{spaced_binary}")

  def write_register(self, c, r, reg, value):
    """
    Writes to a register in a tile.

    Args:
      c (int): Column of the tile.
      r (int): Row of the tile.
      reg (int): Register offset.
      value (int): Value to write.

    Returns:
      None
    """
    return self.binding.write_register(c, r, reg, value)

  def single_step(self, num_instr=1):
    """
    Single step the core if it's in debug mode.

    Args:
      num_instr (int, optional): Number of instructions to step (default 1).

    Returns:
      None
    """
    return self.binding.single_step(num_instr)

  def read_aie_regs(self, reg):
    """
    Reads a register in all debug AIE cores.

    Args:
      reg (int): Register offset.

    Returns:
      list[int]: List of register values for all debug tiles.
    """
    return self.binding.read_regs(reg)

  def write_aie_regs(self, reg, value):
    """
    Writes a register in all debug AIE cores.

    Args:
      reg (int): Register offset.
      value (int): Value to write to all tiles.

    Returns:
      None
    """
    return self.binding.write_regs(reg, value)
