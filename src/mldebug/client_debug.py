# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Top Level Debug implementation.
Calls backend specific code to talk to AIE Hardware.

ClientDebug acts as a facade, delegating execution, memory dumps, and
stamp scheduling to specialized components while maintaining the public API
expected by mldebug_cli and interactive prompts.
"""

import sys

from mldebug.aie_status import AIEStatus
from mldebug.aie_util import AIEUtil
from mldebug.backend.factory import BackendConfig, create_backend
from mldebug.batch_runner import BatchRunner
from mldebug.debug_state import DebugState
from mldebug.debug_server import DebugServer
from mldebug.interactive_controller import InteractiveController
from mldebug.layer_info import LayerInfo
from mldebug.memory_dumper import MemoryDumper
from mldebug.utils import LOGGER


class ClientDebug:
  """
  Top Client Debug Handler for MLDebugger.

  Coordinates the backend implementations, AIE utilities, debugging state, and output directories.
  Delegates execution flow, memory dumps, and stamp scheduling to specialized components.
  Provides utility and step functions for automated and interactive AIE debugging.
  """

  def __init__(self, args, ctx_id, pid, output_dir):
    """
    Initialize the ClientDebug object with current arguments, context, and design state.

    Args:
      args: Parsed command-line arguments.
      ctx_id: Context ID for hardware or simulation.
      pid: Process ID if required by backend.
      output_dir: Directory where debug outputs will be written.
    """
    self.args = args
    self.output_dir = output_dir
    self.aie_utls = []
    self.impls = []
    debug_server = None

    # Create this first so that connection will be aborted in case of crash
    if self.args.automated_debug or self.args.l3:
      debug_server = DebugServer(self.args.subgraph_name, self.output_dir, self.args.backend == "test")

    try:
      self.design_info = LayerInfo(args)
      self.state = DebugState(self.design_info.layers, self.design_info.overlay.get_stampcount())
    except Exception as err:
      if debug_server:
        print("[INFO] closing debug server.")
        debug_server.close()
      raise(err)

    for i in self.design_info.overlay.get_stampids():
      config = BackendConfig(
        tiles=self.design_info.overlay.get_tiles(args.aie_iface.AIE_TILE_T, i),
        ctx_id=ctx_id,
        pid=pid,
        device=args.device,
        design_info=self.design_info,
        args=args,
        core_dump_file=getattr(args, 'core_dump', None),
        no_header=getattr(args, 'no_header', False),
      )
      impl = create_backend(args.backend, config)
      self.impls.append(impl)
      self.aie_utls.append(
        AIEUtil(
          args.aie_iface, impl, self.design_info.overlay.get_tiles(stamp_id=i), self.design_info.work_dir.globals[i]
        )
      )

    self.impl = self.impls[0]
    self.status_handle = AIEStatus(
      self.impl, self.design_info.overlay.get_tiles, args.aie_iface, self.design_info.overlay.get_repr()
    )

    # Initialize specialized components (share mutable lists by reference)
    self.dumper = MemoryDumper(args, output_dir, self.design_info, self.state, self.impl)
    if debug_server:
      self.dumper.debug_server = debug_server

    self.runner = BatchRunner(
      args, self.state, self.design_info, self.impls, self.aie_utls,
      self.dumper, self.status_handle
    )
    self.interactive = InteractiveController(
      args, self.state, self.design_info, self.impls, self.aie_utls, self.runner
    )

    if (self.args.automated_debug or self.args.l3) and not self.design_info.layers:
      print("[ERROR] No layers with kernels found in the design. Exiting Now.")
      self.dumper.debug_server.close()
      sys.exit(0)

  # --- Batch mode delegation ---

  def execute_and_dump(self):
    """
    Execute all layers in batch mode, dumping buffers as required.

    This is the primary entry point for batch mode execution in MLDebugger.
    """
    self.runner.execute_and_dump()

  # --- Interactive mode delegation ---

  def initialize_aie(self):
    """
    Run initial AIE common setup, advance to layer 0, and schedule startup.

    Returns:
      True if initialization completed.
    """
    return self.interactive.initialize_aie()

  def step_iter_manual(self):
    """Step a single iteration manually in console (non-auto) mode."""
    self.interactive.step_iter_manual()

  def step_iteration(self, auto_mode):
    """
    Step a single iteration.

    Args:
      auto_mode: Boolean. If True, disables console log message after stepping.

    Returns:
      True if successful, False otherwise.
    """
    return self.interactive.step_iteration(auto_mode)

  def step_layer(self):
    """
    Step (advance) to the start of the next layer at the first iteration.

    Returns:
      True if successful, False otherwise.
    """
    return self.interactive.step_layer()

  def add_breakpoint(self, layer_num, iteration=1):
    """
    Set a breakpoint at the specified layer and/or iteration.

    Args:
      layer_num: Integer layer number.
      iteration: Integer iteration index (default 1).
    """
    self.interactive.add_breakpoint(layer_num, iteration)

  def continue_execution(self):
    """Continue execution until the next manual breakpoint or to the end of design."""
    self.interactive.continue_execution()

  # --- Memory dump delegation ---

  def dump_memory(self):
    """Dump all buffers for the current layer (L1, L2 inputs/weights/output)."""
    self.dumper.dump_memory_all()

  def dump_l3_buffers_manual(self, name, offset, size):
    """
    Manually dump a specified L3 buffer.

    Args:
      name: Name of the L3 buffer.
      offset: Offset for the L3 buffer.
      size: Size (in bytes) of the L3 buffer to dump.
    """
    self.dumper.dump_l3_buffers_manual(name, offset, size)

  def dump_l3_buffers_interactive(self):
    """Dump all L3 buffers of the current layer interactively."""
    self.dumper.dump_l3_buffers_interactive()

  # --- Stamp/breakpoint delegation ---

  def set_pc_breakpoint(self, pc, slot, sid=0):
    """
    Check and set a PC breakpoint at the given address and slot for the selected stamp.

    Args:
      pc: Integer program counter value where breakpoint is set.
      slot: Which slot to set (0 = start, 1 = end).
      sid: Stamp id.

    Returns:
      Result of backend breakpoint call.

    Raises:
      RuntimeError: For invalid configuration.
    """
    return self.runner.set_pc_breakpoint(pc, slot, sid)

  # --- Inspection (stays on ClientDebug) ---

  def print_current_state(self, layer_order=None):
    """
    Print concise information about the current state of execution.

    Args:
      layer_order: If provided, print info on that layer rather than current.
    """
    sep = "--------------------------------------------"
    if layer_order is not None:
      info_layer = self.state.get_layer_by_order(layer_order)
      if info_layer:
        print(f"{sep}\nInformation on layer: {layer_order}\n{sep}")
        print(info_layer)
        print(sep)
      else:
        print(f"Layer not found: {layer_order}. Note: TG Layers aren't supported.")
      return

    self.design_info.print_info()
    if self.args.aie_only:
      return

    layer = self.state.get_current_layer()
    if layer:
      stamp_names = ", ".join([f"Stamp {i}: {stamp.name}" for i, stamp in enumerate(layer.stamps)])
      LOGGER.log(f"Stopped at Start of Kernel(s): {stamp_names}")
      LOGGER.log(f"Current Layer: {layer.layer_order}, Iteration: {self.state.cur_it}")
      LOGGER.log(str(layer))

  def read_lcp(self, col=None, row=None, ping=1):
    """
    Read and print the value of the LCP register for a specific AIE tile.

    Args:
      col: Tile column (optional; if not specified, defaults to first tile).
      row: Tile row (optional; if not specified, defaults to first tile).
      ping: Boolean (1/0) for ping/pong register.

    Returns:
      LCP register value (list or int) or empty list if not found.
    """
    tiles = self.design_info.overlay.get_tiles(self.args.aie_iface.AIE_TILE_T, raw=True)
    if col is None or row is None or (col, row) not in tiles:
      col, row = tiles[0]
    for sid, utl in enumerate(self.aie_utls):
      if (col, row) in utl.tiles:
        print(f"Reading LCP Value for Tile ({col}, {row}) on stamp {sid}:")
        return utl.read_lcp(col, row, ping)
    print("Unable to read")
    return []

  def read_all_core_pc(self):
    """Read and display the program counter (PC) value for all cores on all stamps."""
    for sid, impl in enumerate(self.impls):
      print(f"\n=== Stamp {sid} Core PC ===")
      impl.read_all_core_pc()

  #
  # START Advanced Mode Specific functionality
  #

  def init_leftmost_stamp(self):
    """
    Initialize the leftmost stamp (stamp 0) by enabling PC halt.
    For stamps with index > 1, initialize the stamp and continue execution.
    """
    self.impls[0].enable_pc_halt()
    for sid, impl in enumerate(self.impls):
      if sid > 0:
        self.aie_utls[sid].initialize_stamp()
        impl.continue_aie()

  def wreg_stamp(self, offset, val, sid=0):
    """
    Write all registers in the specified stamp. Leftmost stamp ID is 0.

    Args:
      offset (int): Register offset to write to.
      val (int): Value to be written.
      sid (int, optional): Index of the stamp to write to. Default is 0.

    Raises:
      RuntimeError: If the specified stamp ID is invalid.
    """
    if sid > (len(self.impls) - 1):
      raise RuntimeError(f"Invalid Stamp: {sid}")
    self.aie_utls[sid].write_aie_regs(offset, val)

  def rreg_stamp(self, offset, sid=0):
    """
    Read all registers in the specified stamp. Leftmost stamp ID is 0.

    Args:
      offset (int): Register offset to read from.
      sid (int, optional): Index of the stamp to read from. Default is 0.

    Returns:
      Any: The register value(s) read.

    Raises:
      RuntimeError: If the specified stamp ID is invalid.
    """
    if sid > (len(self.impls) - 1):
      raise RuntimeError(f"Invalid Stamp: {sid}")
    return self.aie_utls[sid].read_aie_regs(offset)

  def print_core_summary(self):
    """Print the core summary for all stamps."""
    self.status_handle.print_core_summary()
    for sid, impl in enumerate(self.impls):
      print(f"\n=== Stamp {sid} PC Breakpoints ===")
      impl.print_pc_breakpoints()
    self.print_current_state()
    print("[INFO] Currently only leftmost stamp is supported for advanced debug.")

  def set_pc_breakpoint_manual(self, pc, slot, sid=0):
    """
    Manual version of PC Breakpoint. Disables right stamps for now.

    Args:
      pc: Integer program counter value where breakpoint is set.
      slot: Which slot to set (0 = start, 1 = end).
      sid: Stamp id.

    Returns:
      Result of backend breakpoint call.

    Raises:
      RuntimeError: For invalid configuration.
    """
    self.init_leftmost_stamp()
    print("[INFO] Breakpoints are only supported on leftmost stamp currently.")
    return self.set_pc_breakpoint(pc, slot, sid)

  def clear_pc_breakpoint_manual(self, slot=None, sid=0):
    """
    Clear PC breakpoints on the leftmost stamp.

    Clears the PC_EVENT register(s) and the backend's internal tracking.
    When both slots are cleared, also zeros DEBUG_CONTROL1 to disable
    the halt-on-event mechanism and calls disable_pc_halt.

    Args:
      slot: Breakpoint slot to clear (0 or 1). If None, clears both slots.
      sid: Stamp id.
    """
    self.init_leftmost_stamp()
    if slot is None:
      self.impls[sid].clear_pc_breakpoint(0)
      self.impls[sid].clear_pc_breakpoint(1)
      self.impls[sid].disable_pc_halt()
      print("[INFO] Cleared PC breakpoints on slots 0 and 1 and disabled halt.")
    else:
      self.impls[sid].clear_pc_breakpoint(slot)
      if not any(self.impls[sid].pc_brkpts):
        self.impls[sid].disable_pc_halt()
        print(f"[INFO] Cleared PC breakpoint on slot {slot} and disabled halt.")
      else:
        print(f"[INFO] Cleared PC breakpoint on slot {slot}.")

  def read_core_pc_manual(self, sid=0):
    """
    Read the core program counter from all AIE tiles on a specific stamp.

    Args:
      sid: Stamp id.

    Returns:
      Dict of core program counters.
    """
    print(f"Reading Core PC for Stamp {sid}")
    if sid > (len(self.impls) - 1):
      raise RuntimeError(f"Invalid Stamp: {sid}")
    return self.aie_utls[sid].read_core_pc()

  def goto_pc(self, pc):
    """
    Set a breakpoint at the specified PC and continue execution until that PC is hit.

    Args:
      pc: Integer program counter to break at.
    """
    self.impl.enable_pc_halt()
    self.set_pc_breakpoint(pc, 0)
    self.runner.hit_next_breakpoint()
    hit_pc = self.impl.read_core_pc()
    print(f"Stopped at PC : {hit_pc}")

  #
  # END Advanced Mode Specific functionality
  #
