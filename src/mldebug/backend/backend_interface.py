# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Backend Interface - Abstract Base Class for all backend implementations
"""

from abc import ABC, abstractmethod


class BackendInterface(ABC):
  """
  Abstract base class that defines the interface for all backend implementations.
  All backend implementations (XRT, Test, CoreDump) must inherit from this class.

  Capability flags (override in subclasses as needed):
    is_simulation: True if backend simulates execution without real hardware (e.g. TestImpl).
    is_offline: True if backend reads from static data, not live hardware (e.g. CoreDumpImpl).
  """

  is_simulation: bool = False
  is_offline: bool = False

  @abstractmethod
  def __init__(self, *args, **kwargs):
    """
    Initialize the backend implementation.

    Constructor signatures vary per backend.  Use the factory to create
    instances::

        from mldebug.backend.factory import BackendConfig, create_backend
        config = BackendConfig(tiles=tiles, ctx_id=ctx_id, ...)
        impl = create_backend("xrt", config)
    """

  @abstractmethod
  def read_core_debug_status(self):
    """
    Reads the core debug status
    """

  @abstractmethod
  def read_core_execution_status(self):
    """
    Reads the core execution status
    """

  @abstractmethod
  def poll_core_status(self) -> int:
    """
    Polls the core debug status. Returns 1 if core is halted
    """

  @abstractmethod
  def configure_performance_counters(self):
    """
    Configure performance counters
    """

  @abstractmethod
  def set_performance_counter_halt(self):
    """
    Set performance counter halt
    """

  @abstractmethod
  def read_core_pc(self, all_tiles=False) -> list[int] | int:
    """
    Reads the current core Program Counter line

    Args:
      all_tiles: If True, return PC for all tiles; otherwise return for first tile
    """

  @abstractmethod
  def read_all_core_pc(self):
    """
    Reads the current core Program Counter line for all tiles
    """

  @abstractmethod
  def continue_aie(self):
    """
    Un-halts the AIE and resumes execution
    """

  @abstractmethod
  def set_pc_breakpoint(self, pc_value, idx=0):
    """
    Enables creation of a valid Program Counter event when the
    given instruction line number is reached

    Args:
      pc_value: Program counter value for breakpoint
      idx: Breakpoint index (0 or 1)
    """

  @abstractmethod
  def clear_pc_breakpoint(self, idx=0):
    """
    Clear PC breakpoint

    Args:
      idx: Breakpoint index (0 or 1)
    """

  @abstractmethod
  def print_pc_breakpoints(self):
    """
    Print currently configured pc events
    """

  @abstractmethod
  def enable_pc_halt(self):
    """
    Sets a breakpoint (Instructs the AIE to halt when the program counter event is hit)
    """

  @abstractmethod
  def get_pc(self) -> list[int] | int:
    """
    Reads the current PC Value from a tile
    """

  @abstractmethod
  def disable_pc_halt(self):
    """
    Disable PC halt
    """

  @abstractmethod
  def dump_memory(self, c, r, buffer_offset, buffer_size, filename=None) -> list[int]:
    """
    Read and return L1/L2 memory as a list

    Args:
      c (int): column
      r (int): row
      buffer_offset (int): memory offset
      buffer_size (int): size of memory to read in bytes
      filename (str): optionally dump the memory to a file
    """

  @abstractmethod
  def read_register(self, c, r, reg) -> int:
    """
    Reads a register from a tile

    Args:
      c (int): column
      r (int): row
      reg (int): register offset
    """

  @abstractmethod
  def print_register(self, c, r, reg):
    """
    Read and display register for humans

    Args:
      c (int): column
      r (int): row
      reg (int): register offset
    """

  @abstractmethod
  def write_register(self, c, r, reg, value):
    """
    Writes to a register in a tile

    Args:
      c (int): column
      r (int): row
      reg (int): register offset
      value (int): Value to write into the register
    """

  @abstractmethod
  def single_step(self, num_instr=1):
    """
    Single step the core if it's in debug mode

    Args:
      num_instr: Number of instructions to step
    """

  @abstractmethod
  def read_aie_regs(self, reg)-> list[int]:
    """
    Reads a register in all of debug aie cores

    Args:
      reg: Register offset
    """

  @abstractmethod
  def write_aie_regs(self, reg, value):
    """
    Write a register in all of debug aie cores

    Args:
      reg: Register offset
      value: Value to write
    """
