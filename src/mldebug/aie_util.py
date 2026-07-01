# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Manages high level interaction with AIE
"""

from mldebug.utils import LOGGER, wait_until


class AIEUtil:
  """
  AIE Utility class
  Provides high-level control and inspection of AIE core and overlay
  """

  def __init__(self, aie_iface, impl, tiles, aie_globals):
    """
    Initialize AIEUtil with hardware interface, backend implementation, tile list, and globals.

    Args:
      aie_iface: Architecture-specific AIE interface with register maps and helpers.
      impl: Backend implementation object (e.g., XRT or TestImpl).
      tiles: List of (column, row) tuples for tile positions to operate on.
      aie_globals: List of global variable descriptors from work dir ELF analysis.
    """
    self.impl = impl
    self.aie_iface = aie_iface
    self.pm_reload_count = 0
    self.globals = aie_globals
    self.tiles = tiles
    self.pm_tracker_tile = self._filter_tiles(self.aie_iface.AIE_TILE_T)[0]
    self._pm_tracker_initialized = False
    self._error_found = False

  def set_globals(self, aie_globals):
    """
    Updates the internal list of AIE global variables (e.g., after work directory change).

    Args:
      aie_globals: New list of global variable descriptors.
    """
    self.globals = aie_globals

  def read_lcp(self, c, r, ping=False):
    """
    Read the LCP ping or pong variable from the global symbols and dump its memory contents.

    Args:
      c (int): Column index of tile to read from.
      r (int): Row index of tile to read from.
      ping (bool): If True, read 'lcpPing'. Otherwise, read 'lcpPong'.

    Returns:
      list[int]: The dumped memory contents of the LCP global variable.
    """
    var_name = "lcpPing" if ping else "lcpPong"
    if self.globals:
      for var in self.globals:
        if var.name == var_name:
          phy_addr = var.address & self.aie_iface.LOGICAL_DM_MASK
          print(f"Reading {var_name} from globals: {hex(phy_addr)}, {var.size}")
          return self.impl.dump_memory(c, r, phy_addr, var.size)
    print(f"Globals not available or {var_name} not found.")
    return []

  def _is_test_mode(self):
    """
    Return True if running in test/CI mode with a simulated backend.

    Returns:
      bool: True if backend simulates execution, else False.
    """
    return self.impl.is_simulation

  def _read_ref_tile(self, offset):
    """
    Read a register at a given offset from the primary PM tracker tile.

    Args:
      offset (int): Register offset.

    Returns:
      int: Raw value read from the register.
    """
    c, r = self.pm_tracker_tile
    return self.impl.read_register(c, r, offset)

  def _write_ref_tile(self, offset, data):
    """
    Write a value to a register at a given offset on the primary PM tracker tile.

    Args:
      offset (int): Register offset to write to.
      data (int): Value to be written.
    Returns:
      None
    """
    c, r = self.pm_tracker_tile
    return self.impl.write_register(c, r, offset, data)

  def _get_eventid(self, event_string):
    """
    Get the numeric event ID for a symbolic event string.

    Args:
      event_string (str): Symbolic event name.

    Returns:
      int: Corresponding event ID for the event string.
    """
    return self.aie_iface.core_event_strings.index(event_string)

  def configure_aiecompiler_layer_halt(self):
    """
    Configure debug halt at the end of each aiecompiler layer by setting DEBUG_CONTROL1 appropriately.
    """
    LOGGER.log("Configuring debug halt at each aiecompiler layer.")
    self.impl.write_aie_regs(self.aie_iface.Core_registers["DEBUG_CONTROL1"], 125 << 16)

  def init_skip_iterations(self):
    """
    Prepare the AIE to skip all iterations for this run by setting up performance counter 0
    to count program counter events (PC_0_CORE).
    """
    reg_map = self.aie_iface.Core_registers
    pc_event = self._get_eventid("PC_0_CORE")

    perf_control = self._read_ref_tile(reg_map["PERF_CNTR_0_CTRL"])
    perf_control &= 0x0000FFFF
    perf_control |= (pc_event << 24) + (pc_event << 16)
    self.impl.write_aie_regs(reg_map["PERF_CNTR_0_CTRL"], perf_control)

  def skip_iterations(self, count, sid):
    """
    Skip a user-specified number of iterations directly in the AIE core using performance counters.

    Args:
      count (int): The number of iterations to skip.
      sid (str): Session or stamp identifier (for error messages).

    Returns:
      bool: True if iterations skipped successfully, False if timeout/hang detected.
    """
    if self._is_test_mode() or count == 0:
      return True

    reg_map = self.aie_iface.Core_registers
    write = self.impl.write_aie_regs

    # Reset counter
    write(reg_map["PERF_CNTR_1"], 0)
    # Step1: Generate event after "count" iterations
    write(reg_map["PERF_CNTR_1_EVENT_VAL"], count)
    # Step2: Configure breakpoint on the perf counter event
    perf_cntr_event = self._get_eventid("PERF_CNT_1_CORE")
    write(reg_map["DEBUG_CONTROL1"], perf_cntr_event << 16)
    self.impl.continue_aie()
    # Step3: Poll all tiles until every PERF_CNTR_1 reaches the specified count.
    perf_cntr_1 = reg_map["PERF_CNTR_1"]
    last = {}

    def reached():
      last["values"] = self.read_aie_regs(perf_cntr_1)
      return all(v == count for v in last["values"].values())

    def on_timeout():
      LOGGER.log(
        f"{sid}: Timeout waiting for skip {count} iterations across tiles! "
        f"Design might be hung. Values={last['values']}"
      )

    if not wait_until(reached, on_timeout=on_timeout):
      return False

    # Step6: Reset debug control to stop at program counter event
    pc_event = self._get_eventid("PC_0_CORE")
    write(reg_map["DEBUG_CONTROL1"], pc_event << 16)
    return True

  def skip_iterations_to_lock_acq(self, lock_acq_pc, count, sid):
    """
    Skip iterations without using counter
    """
    if self._is_test_mode() or count == 0:
      return True

    self.impl.set_pc_breakpoint(lock_acq_pc)
    self.impl.continue_aie()
    wait_until(self.impl.poll_core_status)

    pcs = self.impl.read_core_pc(True)
    is_valid = self.pcs_match_target(pcs, lock_acq_pc)
    if not is_valid:
      LOGGER.log(
        f"{sid}: Invalid result in skip_iterations_to_lock_acq. target_pc={lock_acq_pc} pcs={pcs} "
      )
    # else:
    #  LOGGER.log(
    #      f"{sid}: Successfully skipped to lock acq pc. "
    #      f"target_pc={lock_acq_pc} pcs={pcs} "
    #    )
    return is_valid

  def read_performance_counters(self, c, r):
    """
    Read and display the values and configuration registers of all performance counters
    for a specified tile.

    Args:
      c (int): Column index of the tile.
      r (int): Row index of the tile.

    Returns:
      None
    """

    def get_perf_counter_regs(registers):
      """
      Helper: Extract lists of performance counter register addresses (value and config).

      Args:
        registers (dict): Register map for a tile type.

      Returns:
        tuple[list[int], list[int]]: (List of config addresses, list of value addresses)
      """
      pc_ss_regs = []
      pc_regs = []
      for key in registers.keys():
        if "PERF_CNTR" in key and "CTRL" in key:
          pc_ss_regs.append(registers[key])
        elif "PERF_CNTR" in key:
          pc_regs.append(registers[key])

      return sorted(pc_ss_regs), sorted(pc_regs)

    pc_ss_regs = []
    pc_regs = []
    if (c, r) in self._filter_tiles(self.aie_iface.AIE_TILE_T):
      pc_ss_regs, pc_regs = get_perf_counter_regs(self.aie_iface.Core_registers)
    elif (c, r) in self._filter_tiles(self.aie_iface.MEM_TILE_T):
      pc_ss_regs, pc_regs = get_perf_counter_regs(self.aie_iface.Memory_tile_registers)
    elif (c, r) in self._filter_tiles(self.aie_iface.SHIM_TILE_T):
      pc_ss_regs, pc_regs = get_perf_counter_regs(self.aie_iface.Shim_tile_registers)
    else:
      print("Invalid Tile")
      return
    for i, reg in enumerate(pc_ss_regs):
      print(f"PC {i + 1}-{i} Start/Stop {c},{r}: {hex(self.impl.read_register(c, r, reg))}")
    for i, reg in enumerate(pc_regs):
      print(f"PC{i} Value {c},{r}: {hex(self.impl.read_register(c, r, reg))}")

  def _filter_tiles(self, tile_type):
    """
    Filter the set of overlay tiles by the given tile type.

    Args:
      tile_type (str): One of AIE_TILE_T, MEM_TILE_T, or SHIM_TILE_T.

    Returns:
      list[tuple]: List of (column, row) tuples matching the tile type.
    """
    return self.aie_iface.filter_tiles(tile_type, self.tiles)

  def read_control_instr(self):
    """
    Read and return the value of the SPARE_REG control instruction from all memory tiles.

    Returns:
      dict[str, int]: Mapping of "MEM_TILE_{col}" to the SPARE_REG value for each memory tile.
    """
    spare_reg = self.aie_iface.Memory_tile_registers["SPARE_REG"]
    return {
      f"MEM_TILE_{c}{r}": self.impl.read_register(c, r, spare_reg)
      for c, r in self._filter_tiles(self.aie_iface.MEM_TILE_T)
    }

  def initialize_stamp(self, tiles=None):
    """
    Clear DEBUG_CONTROL1 unhalt specified tiles.

    Args:
      tiles (list[tuple], optional): (col, row) tiles to clear. Default: this stamp's tiles.
    """
    if tiles is None:
      tiles = self.tiles
    for c, r in self.aie_iface.filter_tiles(self.aie_iface.AIE_TILE_T, tiles):
      self.impl.write_register(c, r, self.aie_iface.Core_registers["DEBUG_CONTROL1"], 0)
      self.impl.write_register(c, r, self.aie_iface.Core_registers["DEBUG_CONTROL0"], 0)

  def break_combo(self):
    """
    Set up a debug combo event break by configuring appropriate edge and combo events.
    Used for advanced debugging situations that synchronize state machines with breakpoints.
    """
    reg_map = self.aie_iface.Core_registers
    # setup edge events
    disabled_event = self._get_eventid("DISABLED_CORE")
    rising_edge_control = disabled_event + (1 << 9)
    self.impl.write_aie_regs(reg_map["EDGE_EVENT_CTRL"], rising_edge_control)
    # Setup combo
    pc_event = self._get_eventid("PC_0_CORE")
    rising_edge_event = self._get_eventid("EDGE_DETECTION_EVENT_0_CORE")
    combo_3_event = self._get_eventid("COMBO_EVENT_3_CORE")
    true_core_event = self._get_eventid("TRUE_CORE")

    # eventC==eventD means generate combo3 and reset state machine
    combo_event_inputs = (
      rising_edge_event + (true_core_event << 8) + (pc_event << 16) + (pc_event << 24)
    )
    self.impl.write_aie_regs(reg_map["DEBUG_CONTROL1"], combo_3_event << 16)
    self.impl.write_aie_regs(reg_map["COMBO_EVENT_INPUTS_A_D"], combo_event_inputs)

  def set_fsp_breakpoint(self):
    """
    Set a breakpoint at the lock acquire event in all AIE cores. Used for failsafe partition (FSP) transitions.
    """
    self.impl.write_aie_regs(self.aie_iface.Core_registers["DEBUG_CONTROL1"], 0x2C << 16)

  def clear_pc_breakpoint(self, slot):
    """
    Clear the PC breakpoint in all AIE tiles for the requested breakpoint slot.

    Args:
      slot (int): Which PC breakpoint slot (0 or 1) to clear.
    """
    if slot not in [0, 1]:
      return

    for c, r in self._filter_tiles(self.aie_iface.AIE_TILE_T):
      self.impl.write_register(c, r, self.aie_iface.Core_registers["PC_EVENT0"] + 4 * slot, 0)

  def check_errors(self, layer, itr):
    """
    Check all AIE compute tiles for integer or floating-point overflow/underflow events after a layer/iteration.

    Args:
      layer (int): The layer being debugged (for reporting).
      itr (int): The iteration within the layer (for reporting).

    Side Effects:
      - Prints diagnostic summary if any tile error is detected.
      - Sets internal error flag so subsequent checks are skipped.
    """
    if self._error_found:
      return
    aif = self.aie_iface
    bad_tiles = {}

    # Check primary error event register
    for c, r in self._filter_tiles(aif.AIE_TILE_T):
      data = self.impl.read_register(c, r, aif.Core_registers[aif.ERRORS_EVENT_REG])
      parsed = aif.parse_register(aif.ERRORS_EVENT_REG, data)
      for estr in aif.errors_event_strings:
        if estr in parsed:
          if (c, r) not in bad_tiles:
            bad_tiles[(c, r)] = []
          bad_tiles[(c, r)].append(estr)
          self._error_found = True

    # Check secondary error event register if it exists (NPU3 only)
    if hasattr(aif, "ERRORS_EVENT_REG2"):
      for c, r in self._filter_tiles(aif.AIE_TILE_T):
        data = self.impl.read_register(c, r, aif.Core_registers[aif.ERRORS_EVENT_REG2])
        parsed = aif.parse_register(aif.ERRORS_EVENT_REG2, data)
        for estr in aif.errors_event_strings:
          if estr in parsed:
            if (c, r) not in bad_tiles:
              bad_tiles[(c, r)] = []
            bad_tiles[(c, r)].append(estr)
            self._error_found = True

    if self._error_found:
      # Invert mapping: group tiles by the error event string they reported.
      error_summary = {}
      for tile, estrs in bad_tiles.items():
        for estr in estrs:
          error_summary.setdefault(estr, []).append(tile)

      # Coalesce events that share the exact same tile list into one entry.
      tiles_to_events = {}
      for estr, tiles in error_summary.items():
        key = tuple(sorted(tiles))
        tiles_to_events.setdefault(key, []).append(estr)

      summary_str = "\n".join(
        f"  {', '.join(sorted(estrs))}: {list(tiles)}"
        for tiles, estrs in sorted(tiles_to_events.items(), key=lambda kv: sorted(kv[1]))
      )
      print(
        f"\n\n[WARNING] AIE Core error event detected at previous layer/iteration."
        f" Current State: Start of Layer_{layer}, It_{itr}\nERROR_EVENTS->CORES SUMMARY:\n{summary_str}\n"
      )
      print()

  def write_aie_regs(self, offset, val):
    """
    Write a value to all AIE tile registers

    Args:
      offset (int): Register offset to write to.
      data (int): Value to be written.
    Returns:
      None
    """
    for c, r in self._filter_tiles(self.aie_iface.AIE_TILE_T):
      self.impl.write_register(c, r, offset, val)

  def read_aie_regs(self, offset):
    """
    Read all AIE tile registers

    Args:
      offset (int): Register offset to read from.
    Returns:
      int: Raw value read from the register.
    """
    retdict = {}
    for c, r in self._filter_tiles(self.aie_iface.AIE_TILE_T):
      retdict[(c, r)] = self.impl.read_register(c, r, offset)
    return retdict

  def read_core_pc(self):
    """
    Read the core program counter from all AIE tiles
    """
    return self.read_aie_regs(self.aie_iface.Core_registers["CORE_PC"])

  def read_core_pc_dict(self):
    """
    Read the core program counter from all AIE tiles
    """
    return self.read_aie_regs(self.aie_iface.Core_registers["CORE_PC"])

  def read_core_pc_tile(self, c, r):
    """
    Read the core program counter from all AIE tiles
    """
    return self.impl.read_register(c, r, self.aie_iface.Core_registers["CORE_PC"])

  def single_step_core(self, c, r):
    """
    Single step an aie core
    """
    offset = self.aie_iface.Core_registers["DEBUG_CONTROL0"]
    self.impl.write_register(c, r, offset, (1 << 2))

  def disable_ecc_event(self):
    """
    Disable ECC Event for this stamp
    """
    if not self.aie_iface.Core_registers.get("ECC_SCRUB_EVENT"):
      return
    for c, r in self._filter_tiles(self.aie_iface.AIE_TILE_T):
      self.impl.write_register(c, r, self.aie_iface.Core_registers["ECC_SCRUB_EVENT"], 0)

  def pcs_match_target(self, pcs, target_pc, allow_combo_delay=False):
    """
    PC matching utility
    """
    # AIE PC can lag the breakpoint by 1-2 cycles; combo events add more delay.
    # 8 cycles is a safe margin for most cases
    num_pipeline_stages = 5
    max_pc_tolerance = 32

    delay_allowed = max_pc_tolerance if allow_combo_delay else 1
    pc_matches = all(abs(pc - target_pc) < delay_allowed for pc in pcs)
    if not pc_matches:
      # some tiles aren't halted
      if not self.impl.poll_core_status():
        return False
      pc_dict = self.read_core_pc_dict()
      for tile, val in pc_dict.items():
        if target_pc == val:
          continue
        # print(f"Try to reconcile tile {tile} {val}")
        col, row = tile
        for _ in range(num_pipeline_stages):
          self.single_step_core(col, row)
          newpc = self.read_core_pc_tile(col, row)
          delta = newpc - target_pc
          if target_pc == newpc or max_pc_tolerance > delta > 0:
            break
        # if core pc is slightly ahead, we should be okay
        # but if not, execution can run into trouble later
        if target_pc > self.read_core_pc_tile(col, row):
          return False
        # print("Successfully reconciled")
    return True
