# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Batch mode execution engine and stamp scheduling for AIE debugging.

Contains the core execution primitives (breakpoint coordination, layer
execution, multi-stamp scheduling) and the batch-mode orchestration loop.
InteractiveController builds on this for interactive stepping.
"""

import pathlib
import sys
import time

from concurrent.futures import ThreadPoolExecutor, as_completed

from mldebug.utils import LOGGER, timeit

# 16 byte pm, we assume 2 clock cycle delay
COMBO_EVENT_MAX_DELAY_CYCLES = 32


class BatchRunner:
  """
  Core execution engine: stamp scheduling, breakpoint coordination,
  layer execution, and batch-mode orchestration.

  Combines stamp scheduling (PC breakpoints, multi-stamp synchronization,
  PM reload detection) with the execution loop that drives layers through
  their iterations.  Used directly for batch mode (execute_and_dump) and
  as the execution backend for InteractiveController.
  """

  def __init__(self, args, state, design_info, impls, aie_utls,
               dumper, status_handle):
    """
    Args:
      args: Parsed command-line arguments.
      state: DebugState tracking execution state.
      design_info: LayerInfo with overlay and work directory metadata.
      impls: List of backend implementation instances (shared, mutable).
      aie_utls: List of AIEUtil instances per stamp (shared, mutable).
      dumper: MemoryDumper for buffer dump operations.
      status_handle: AIEStatus for reading/writing AIE status.
    """
    self.args = args
    self.state = state
    self.design_info = design_info
    self.impls = impls
    self.aie_utls = aie_utls
    self.dumper = dumper
    self.status_handle = status_handle

  # ------------------------------------------------------------------ #
  # Stamp scheduling
  # ------------------------------------------------------------------ #

  def common_init(self):
    """
    Common initialization for batch and interactive modes.

    Collapses to single-stamp mode if multistamp flag is not set,
    enables PC halt for all stamps, and initializes skip-iteration support.
    """
    if not self.args.run_flags.multistamp and self.design_info.overlay.get_stampcount() > 1:
      for layer in self.design_info.layers:
        layer.stamps[:] = layer.stamps[:1]
      for u in self.aie_utls[1:]:
        u.initialize_stamp()
      # In-place list modification so all holders of these references see the change
      del self.aie_utls[1:]
      del self.impls[1:]
      self.design_info.overlay.layout = (1,) + self.design_info.overlay.layout[1:]
      self.design_info.overlay.stamps = {0: self.design_info.overlay.stamps[0]}
      LOGGER.log("[INFO] Using single stamp control. Please use multistamp flag for more data.")

    for sid in self.design_info.overlay.get_stampids():
      self.impls[sid].enable_pc_halt()
      if self.args.run_flags.skip_iter:
        self.aie_utls[sid].init_skip_iterations()

    if self.args.run_flags.skip_iter:
      LOGGER.log("[INFO] All iterations will be skipped for this run.")

  def set_pc_breakpoint(self, pc, slot, sid=0):
    """
    Set a PC breakpoint at the given address and slot for the selected stamp.

    Args:
      pc: Integer program counter value where breakpoint is set.
      slot: Which slot to set (0 = start, 1 = end).
      sid: Stamp id.

    Returns:
      Result of backend breakpoint call.

    Raises:
      RuntimeError: For invalid configuration.
    """
    if pc is None:
      raise RuntimeError("Invalid configuration detected. Please check metadata.")
    return self.impls[sid].set_pc_breakpoint(pc, slot)

  def _set_layer_breakpoint(self, layer, skip_end_pc, sid, pm_reload_expected):
    """
    Set start and (optionally) end PC breakpoints for the specified layer and stamp.

    Args:
      layer: Target Layer object.
      skip_end_pc: Boolean, when True skips end PC breakpoint.
      sid: Stamp id.
      pm_reload_expected: True if PM reload is expected, for break_combo.

    Returns:
      True if breakpoint(s) are set successfully, else False.
    """
    start_pc_slot = 0
    end_pc_slot = 1

    stamp = layer.stamps[sid]
    start_pc = stamp.start_pc
    if not start_pc:
      print(f"Invalid configuration on stamp {sid} layer {layer.layer_order}.")
      return False
    self.set_pc_breakpoint(start_pc, start_pc_slot, sid)

    if pm_reload_expected:
      self.aie_utls[sid].break_combo()

    if skip_end_pc:
      self.aie_utls[sid].clear_pc_breakpoint(end_pc_slot)
    else:
      self.set_pc_breakpoint(stamp.end_pc, end_pc_slot, sid)
    return True

  def check_pm_reload(self, stamp_id=0):
    """
    Check if the next ELF will be loaded (PM Reload).

    Args:
      stamp_id: Stamp index to check for reload (default 0).

    Returns:
      True if program memory reload will occur at the next layer, False otherwise.
    """
    layer = self.state.layers[self.state.current_layer]
    if not self.design_info.work_dir.pm_reload_en[stamp_id] or self.state.current_layer + 1 >= len(self.state.layers):
      return False

    if stamp_id > 0 and not self.design_info.is_batched():
      next_layer = self.state.get_next_layer_for_stamp(stamp_id, idx=1)
    else:
      next_layer = self.state.layers[self.state.current_layer + 1]

    if next_layer and stamp_id < len(layer.stamps) and stamp_id < len(next_layer.stamps):
      return layer.stamps[stamp_id].elf_name != next_layer.stamps[stamp_id].elf_name
    return False

  def hit_next_breakpoint(self, sid=0):
    """
    Run AIE until the next breakpoint is hit for the given stamp.

    Args:
      sid: Stamp id (default 0).
    """
    max_attempts = 1200
    impl = self.impls[sid]

    impl.continue_aie()
    while not impl.poll_core_status() and max_attempts > 0:
      # 20 mins for aiesim
      if max_attempts <= 3 or self.args.aiesim:
        time.sleep(1)
      max_attempts -= 1

  def schedule_layer_start(self, next_layer):
    """
    Schedule and apply breakpoints to reach the first iteration of a new layer
    across all stamps.

    After breakpoints are hit, verifies PC values and invokes start-breakpoint
    processing or error handling.

    Args:
      next_layer: Next Layer object to start.
    """
    stamp_target_layers = {0: next_layer}

    for sid in range(1, len(self.state.pm_reload)):
      stamp_target_layers[sid] = self.state.get_next_layer_for_stamp(sid)

    bes_to_poll = []
    bes_to_run = []
    # Stamp0 breakpoint always scheduled
    # Stamp1+ breakpoint only scheduled at end of 2 stamps or at beginning
    #
    # NOTE ON "EARLY" PM-RELOAD ARMING:
    # `target_layer` for stamp N may be a layer *later* than `next_layer`
    # (the outer-loop layer currently being scheduled). This happens when a
    # non-participating stamp skips one or more layers - `get_next_layer_for_stamp`
    # walks forward to the next layer that actually contains this stamp.
    #
    # When that future target layer uses a different ELF for this stamp, we
    # must arm the start-PC breakpoint AND the combo event (via break_combo
    # inside _set_layer_breakpoint) *before* the stamp is released with
    # continue_aie below. If we defer arming until we reach the outer-loop
    # iteration for the stamp's real target layer, the stamp would have
    # already been released without a valid breakpoint (or without combo
    # event coverage across the PM reload) and would either free-run past
    # its target start PC or stall indefinitely at the end of its previous
    # layer - blocking progress of the other stamps that depend on it.
    #
    # Consequence: the "PM RELOAD" log may appear while scheduling an outer
    # layer that this stamp does not participate in. That is intentional -
    # it marks when the breakpoint is *armed*, not when the reload
    # physically occurs. The post-poll block below finalizes the combo
    # event (enable_pc_halt + clear pm_reload[sid]) only once the outer
    # loop actually reaches that stamp's target layer, guarded by
    # `break_on_stamp_scheduled[sid]` so we do not re-arm on the way there.
    for sid, pml in enumerate(self.state.pm_reload):
      target_layer = stamp_target_layers.get(sid)
      if not target_layer or (sid > 0 and self.state.break_on_stamp_scheduled[sid]):
        continue
      self.state.break_on_stamp_scheduled[sid] = True
      if pml:
        if target_layer.layer_order != next_layer.layer_order:
          LOGGER.log(
            f"\nArming PM RELOAD on stamp {sid} for Layer_{target_layer.layer_order} "
          )
        else:
          LOGGER.log(f"\nPM RELOAD on stamp: {sid}")
      stamp = target_layer.stamps[sid]
      skip_end_pc = not (self.args.run_flags.l1_ofm_dump and stamp.end_pc)
      self._set_layer_breakpoint(target_layer, skip_end_pc, sid, pml)
      bes_to_run.append(self.impls[sid])
      if target_layer.layer_order == next_layer.layer_order:
        bes_to_poll.append(self.impls[sid])

    # Run stamps at exact same time
    for be in bes_to_run:
      be.continue_aie()

    # Poll stamps until breakpoint is hit
    max_attempts = 1200
    while max_attempts > 0:
      if all(be.poll_core_status() for be in bes_to_poll):
        break
      if self.args.backend == "test":
        break
      max_attempts -= 1

    # When combo events are used, it takes a few cycles to
    # hit the breakpoint, so pc might have moved
    for sid, pml in enumerate(self.state.pm_reload):
      ta_layer = stamp_target_layers.get(sid)
      if ta_layer is not None and next_layer.layer_order == ta_layer.layer_order:
        stamp = next_layer.stamps[sid]
        pcs = self.impls[sid].read_core_pc(True)

        # combo event trigger has one cycle delay
        is_correct_pc = all(stamp.start_pc == pc for pc in pcs)
        if not is_correct_pc and pml:
          is_correct_pc = all(pc - stamp.start_pc < COMBO_EVENT_MAX_DELAY_CYCLES for pc in pcs)

        if is_correct_pc:
          self._process_start_breakpoint(next_layer, 1, sid=sid)
        else:
          print(f"[ERROR] Step to start of Layer_{next_layer.layer_order} failed on Stamp_{sid}")
          self._process_err()
        if pml:
          self.impls[sid].enable_pc_halt()
          self.state.pm_reload[sid] = False

  # ------------------------------------------------------------------ #
  # Core execution primitives (shared by batch and interactive)
  # ------------------------------------------------------------------ #

  def _process_err(self):
    """Print error and debugging information due to an invalid or hang state, then exit."""
    LOGGER.log("[ERROR] Invalid State. This could indicate a hang in AIE")
    for sid, impl in enumerate(self.impls):
      LOGGER.log(f"Sid {sid} Core PC : {impl.read_core_pc(True)}")

    LOGGER.log("[INFO] Writing AIE Status to aie_status_error.txt")
    self.design_info.print_info()
    if not self.args.aie_only:
      layer = self.state.get_current_layer()
      if layer:
        stamp_names = ", ".join([f"Stamp {i}: {stamp.name}" for i, stamp in enumerate(layer.stamps)])
        LOGGER.log(f"Stopped at Start of Kernel(s): {stamp_names}")
        LOGGER.log(f"Current Layer: {layer.layer_order}, Iteration: {self.state.cur_it}")
        LOGGER.log(str(layer))

    p = self.args.output_dir
    if p:
      pathlib.Path(p).mkdir(parents=True, exist_ok=True)
      self.status_handle.get(p + "/" + "aie_status_error.txt")
    else:
      self.status_handle.get("aie_status_error.txt")
    sys.exit(1)

  def _process_end_breakpoint(self, layer, it, sid):
    """
    Handle actions at the end breakpoint of a layer iteration.

    Args:
      layer: Current Layer object.
      it: Current iteration number.
      sid: Stamp id.
    """
    if self.args.interactive:
      return

    self.dumper.dump_memory_l1(layer.out_buffers, it, self.state.ofm_ping, sid=sid)
    self.state.ofm_ping = not self.state.ofm_ping

  def _process_start_breakpoint(self, layer, it, sid=0):
    """
    Handle actions at the start breakpoint of a layer iteration.

    Dumps input buffers from present iteration, L2 OFM from previous iteration,
    and optionally L3 buffers depending on VAIML vs X2 flow.

    Args:
      layer: Current Layer object.
      it: Current iteration number.
      sid: Stamp id (default 0).
    """
    first_it = it == 1
    if not self.args.backend == "test":
      LOGGER.log(f"Hit Start of iteration {it}", flush=True, log=first_it)

    for u in self.aie_utls:
      u.check_errors(layer.layer_order, it)
      if self.args.backend == "test":
        break

    if self.args.interactive:
      return

    if self.args.exit_at_layer and layer.layer_order >= self.args.exit_at_layer:
      LOGGER.log(f"[INFO] Exiting debugger at Layer: {layer.layer_order}")
      sys.exit(0)

    if self.args.run_flags.layer_status and first_it:
      self.status_handle.get(self.dumper.get_output_path() + "/aie_status_layer_start.txt")

    # L3 buffer dump: X2 dumps at first iteration, VAIML at last iteration
    if self.args.x2_folder_path is not None and first_it and sid == 0:
      self.dumper.dump_x2_buffers(layer, it)
    elif self.args.vaiml_folder_path is not None and it == layer.lcp.num_iter and sid == 0:
      self.dumper.dump_l3_buffers(layer)

    if self.args.run_flags.skip_dump:
      return

    # L1, L2 buffer dumps
    if self.args.vaiml_folder_path and (it - 1) % layer.lcp.buffer_iter == 0:
      self.dumper.dump_memory_l2(layer.in_buffers, it, sid=sid)
    elif self.args.x2_folder_path:
      self.dumper.dump_memory_l2(layer.in_buffers, it, sid=sid)

    if (it - 1) % layer.lcp.wts_iter == 0:
      self.dumper.dump_memory_l2(layer.wts_buffers, it, sid=sid)
    if self.args.run_flags.l2_ifm_dump:
      return
    self.dumper.dump_memory_l1(layer.in_buffers, it, sid=sid)
    self.dumper.dump_memory_l1(layer.wts_buffers, it, sid=sid)
    if it > 1 and it % layer.lcp.super_iter == 1:
      self.dumper.dump_memory_l2(layer.out_buffers, it, sid=sid)
    elif self.args.x2_folder_path:
      self.dumper.dump_memory_l2(layer.out_buffers, it, sid=sid)

  def _run_stamp(self, layer, sid, target_itr, cur_it=1):
    """
    Execute a layer for a given stamp from current to target iteration.

    Args:
      layer: Layer object.
      sid: Stamp id.
      target_itr: Final iteration number to execute through.
      cur_it: Starting iteration number (default 1).

    Returns:
      True on success, False on error.
    """
    stamp = layer.stamps[sid]
    skip_end_pc = not (self.args.run_flags.l1_ofm_dump and stamp.end_pc)
    if not target_itr:
      target_itr = layer.lcp.num_iter

    if self.args.run_flags.skip_iter:
      self.state.error = not self.aie_utls[sid].skip_iterations(target_itr - cur_it, sid)
    else:
      while cur_it < target_itr:
        self.hit_next_breakpoint(sid)
        all_pc = self.impls[sid].read_core_pc(True)
        if all(stamp.start_pc == pc for pc in all_pc):
          if cur_it % layer.lcp.depth_iter != 0 or skip_end_pc:
            cur_it += 1
          self._process_start_breakpoint(layer, cur_it, sid=sid)
        elif all(stamp.end_pc == pc for pc in all_pc):
          cur_it += 1
          self._process_end_breakpoint(layer, cur_it, sid)
        else:
          print(f"[ERROR] Abort Execution of Stamp {sid}. PC List: {all_pc} doesn't match {stamp.start_pc}")
          self.state.error = True
          break

    if sid == 0:
      self.dumper.dump_l3_buffers(layer)
    return not self.state.error

  def run_layer(self, layer, target_itr=None, cur_it=None):
    """
    Execute the given layer across all stamps using ThreadPoolExecutor.

    Args:
      layer: Layer object to execute.
      target_itr: Target iteration (default None = last).
      cur_it: Initial iteration number (default None = 1).
    """
    n_stamp = len(layer.stamps)
    if not cur_it:
      cur_it = 1

    with ThreadPoolExecutor(max_workers=n_stamp) as executor:
      futures = [executor.submit(self._run_stamp, layer, sid, target_itr, cur_it) for sid in range(n_stamp)]
      for f in as_completed(futures):
        res = f.result()
        if not res:
          self.state.error = True

    # At final iteration of multistamp design, reset scheduling state
    if n_stamp > 1 and (target_itr is None or target_itr == layer.lcp.num_iter):
      for sid, _ in enumerate(self.state.break_on_stamp_scheduled):
        self.state.break_on_stamp_scheduled[sid] = False
      for sid in range(1, n_stamp):
        if not self.state.get_next_layer_for_stamp(sid, idx=1):
          self.impls[sid].continue_aie()

    if self.state.error:
      self._process_err()

  # ------------------------------------------------------------------ #
  # Batch mode entry
  # ------------------------------------------------------------------ #

  @timeit
  def execute_and_dump(self):
    """
    Execute all layers in batch mode, dumping buffers as required.

    Primary entry point for batch mode execution in MLDebugger.
    """
    self.common_init()
    overlay = self.design_info.overlay

    for layer in self.state.update_layer():
      LOGGER.log(f"Stepping to layer {layer.layer_order}: {layer.stamps[0].name},"
                 f" stamps: {len(layer.stamps)}, iters {layer.lcp.num_iter}")
      self.schedule_layer_start(layer)
      self.run_layer(layer)
      for sid in range(len(layer.stamps)):
        self.state.pm_reload[sid] = self.check_pm_reload(sid)

    for sid in overlay.get_stampids():
      self.aie_utls[sid].initialize_stamp()
      self.impls[sid].continue_aie()
    LOGGER.log("\nFinished Execution")
    self._handle_fsp()

  def _handle_fsp(self):
    """Handle end-of-run logic for VAIML Failsafe Partition mode."""
    is_fsp = self.args.vaiml_folder_path and not self.args.last_fsp

    if is_fsp:
      for utl in self.aie_utls:
        utl.set_fsp_breakpoint()

    if self.dumper.debug_server:
      self.dumper.debug_server.close()
    elif is_fsp:
      input(
        "First, please press Enter ONCE in the VAIML process "
        "to load the next Failsafe Partition and wait for "
        "`waiting for user input`. Then press Enter here."
      )
