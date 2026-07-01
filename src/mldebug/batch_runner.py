# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Batch mode execution engine and stamp scheduling for AIE debugging.

Contains the core execution primitives (breakpoint coordination, layer
execution, multi-stamp scheduling) and the batch-mode orchestration loop.
InteractiveController builds on this for interactive stepping.
"""

import dataclasses
import json
import pathlib
import sys
import time

from concurrent.futures import ThreadPoolExecutor, as_completed

from mldebug.utils import LOGGER, cleanup_and_exit, timeit, wait_until


class BatchRunner:
  """
  Core execution engine: stamp scheduling, breakpoint coordination,
  layer execution, and batch-mode orchestration.

  Combines stamp scheduling (PC breakpoints, multi-stamp synchronization,
  PM reload detection) with the execution loop that drives layers through
  their iterations.  Used directly for batch mode (execute_and_dump) and
  as the execution backend for InteractiveController.
  """

  def __init__(self, args, state, design_info, impls, aie_utls, dumper, status_handle):
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
    Enable PC halt and skip-iteration support for each active replica. The
    single-stamp collapse is handled up front by the Overlay.
    """
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

    stamp = layer.get_stamp(sid)
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
    Check if the next ELF will be loaded (PM Reload) for the given replica.
    Args:
      stamp_id: Replica id to check for reload (default 0).
    Returns:
      True if program memory reload will occur at the next layer, False otherwise.
    """
    layer = self.state.layers[self.state.current_layer]
    # PM Load is not enabled for this stamp or this is last layer
    if not self.design_info.work_dir.stamp(
      stamp_id
    ).pm_reload_en or self.state.current_layer + 1 >= len(self.state.layers):
      return False
    # Stamp id doesn't run for this layer
    if not layer.runs_replica(stamp_id):
      return False
    # Find next layer that runs this stamp
    if self.design_info.overlay.is_leftmost_in_batch(stamp_id):
      next_layer = self.state.layers[self.state.current_layer + 1]
    else:
      next_layer = self.state.get_next_layer_for_stamp(stamp_id, idx=1)
    if next_layer is None or not next_layer.runs_replica(stamp_id):
      return False

    cur_stamp = layer.get_stamp(stamp_id)
    next_stamp = next_layer.get_stamp(stamp_id)
    return cur_stamp.elf_name != next_stamp.elf_name

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
    overlay = self.design_info.overlay
    stamp_target_layers = {}
    for sid in range(len(self.state.pm_reload)):
      if overlay.is_leftmost_in_batch(sid):
        # Leftmost replica of every batch always participates in next_layer.
        stamp_target_layers[sid] = next_layer
      else:
        stamp_target_layers[sid] = self.state.get_next_layer_for_stamp(sid)

    for utl in self.aie_utls:
      utl.disable_ecc_event()

    bes_to_poll = []
    bes_to_run = []
    active_stamps_all_batches = []
    # Per-batch leftmost stamps (sid 0 within each batch) always have their
    # breakpoint scheduled on next_layer. The remaining stamps may early-arm
    # a breakpoint for a *future* layer they actually participate in.
    #
    # Example for "EARLY" PM-RELOAD ARMING:
    #   Layer 0  stamp0 stamp1 stamp2
    #   Layer 1  stamp0 stamp1
    #                          <PM Reload Stamp2>
    #   Layer 3  stamp0 stamp1 stamp2
    # Step to layer 0 : step to all 3 stamps
    # Step to layer 1 : run stamp0,1 Arm Stamp2 via combo and continue it
    #                   PM Reload message appears early for stamp 2
    # Step to layer 3 : step to all 3 stamps
    for sid, pml in enumerate(self.state.pm_reload):
      target_layer = stamp_target_layers.get(sid)
      if not target_layer:
        continue
      is_leftmost = overlay.is_leftmost_in_batch(sid)
      reaches_now = target_layer.layer_order == next_layer.layer_order
      already_armed = not is_leftmost and self.state.break_on_stamp_scheduled[sid]
      stamp = target_layer.get_stamp(sid)

      if not already_armed:
        self.state.break_on_stamp_scheduled[sid] = True
        if pml:
          if not reaches_now:
            LOGGER.log(f"\nArming PM RELOAD on stamp {sid} for Layer_{target_layer.layer_order} ")
          else:
            LOGGER.log(f"\nPM RELOAD on stamp: {sid}")
        skip_end_pc = not (self.args.run_flags.l1_ofm_dump and stamp.end_pc)
        self._set_layer_breakpoint(target_layer, skip_end_pc, sid, pml)
        bes_to_run.append(self.impls[sid])

      # We have reached previously scheduled breakpoint
      if reaches_now:
        bes_to_poll.append(self.impls[sid])
        active_stamps_all_batches.append((sid, pml, stamp))

    # Run stamps at exact same time
    for be in bes_to_run:
      be.continue_aie()

    # Poll stamps until breakpoint is hit
    if self.args.backend != "test":
      wait_until(lambda: all(be.poll_core_status() for be in bes_to_poll))

    # Now check that breakpoints were hit at the right PC for each stamp
    # that actually targets next_layer. When combo events are used the PC
    # may have moved by a few cycles past the start_pc.
    for sid, pml, stamp in active_stamps_all_batches:
      pcs = self.impls[sid].read_core_pc(True)
      utl = self.aie_utls[sid]
      is_correct_pc = utl.pcs_match_target(pcs, stamp.start_pc, allow_combo_delay=pml)

      if is_correct_pc:
        self._process_start_breakpoint(next_layer, 1, sid=sid)
      else:
        print(f"[ERROR] Step to start of Layer_{next_layer.layer_order} failed on Stamp_{sid}")
        self._process_err()
      if pml:
        self.impls[sid].enable_pc_halt()
        self.state.pm_reload[sid] = False
      # Breakpoint has now been observed for this stamp;
      self.state.break_on_stamp_scheduled[sid] = False

    # Save for run_layer to consume.
    self.state.active_stamps_all_batches = active_stamps_all_batches

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
        stamp_names = ", ".join(
          [f"Stamp {i}: {stamp.name}" for i, stamp in enumerate(layer.stamps)]
        )
        LOGGER.log(f"Stopped at Start of Kernel(s): {stamp_names}")
        LOGGER.log(f"Current Layer: {layer.layer_order}, Iteration: {self.state.cur_it}")
        LOGGER.log(str(layer))

    p = self.args.output_dir
    if p:
      pathlib.Path(p).mkdir(parents=True, exist_ok=True)
      self.status_handle.get(p + "/" + "aie_status_error.txt")
    else:
      self.status_handle.get("aie_status_error.txt")
    self._write_run_summary("FAIL")
    cleanup_and_exit(self.args, 1)

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
      self._write_run_summary("SUCCESS")
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
      Success or error.
    """
    stamp = layer.get_stamp(sid)
    utl = self.aie_utls[sid]

    skip_end_pc = not (self.args.run_flags.l1_ofm_dump and stamp.end_pc)
    if not target_itr:
      target_itr = layer.lcp.num_iter

    if self.args.run_flags.skip_iter:
      self.state.error = not utl.skip_iterations(target_itr - cur_it, sid)
    elif self.args.run_flags.skip_iter2:
      self.state.error = not utl.skip_iterations_to_lock_acq(
        self.design_info.work_dir.stamp(sid).post_layer_lock_acq_pc, target_itr - cur_it, sid
      )
    else:
      while cur_it < target_itr:
        self.hit_next_breakpoint(sid)
        all_pc = self.impls[sid].read_core_pc(True)
        if utl.pcs_match_target(all_pc, stamp.start_pc):
          if cur_it % layer.lcp.depth_iter != 0 or skip_end_pc:
            cur_it += 1
          self._process_start_breakpoint(layer, cur_it, sid=sid)
        elif utl.pcs_match_target(all_pc, stamp.end_pc):
          cur_it += 1
          self._process_end_breakpoint(layer, cur_it, sid)
        else:
          print(
            f"[ERROR] Abort Execution of Stamp {sid}. PC List: {all_pc} doesn't match {stamp.start_pc}"
          )
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
    if not cur_it:
      cur_it = 1

    # active_stamps_all_batches is determined by schedule_layer_start
    stamps = self.state.active_stamps_all_batches

    with ThreadPoolExecutor(max_workers=len(stamps)) as executor:
      futures = [
        executor.submit(self._run_stamp, layer, sid, target_itr, cur_it)
        for sid, _pml, _stamp in stamps
      ]
      for f in as_completed(futures):
        res = f.result()
        if not res:
          self.state.error = True

    # Unhalt right replicas that have no remaining future layer
    overlay = self.design_info.overlay
    total_replicas = len(self.state.pm_reload)
    if total_replicas > 1 and (target_itr is None or target_itr == layer.lcp.num_iter):
      for sid in range(total_replicas):
        if overlay.is_leftmost_in_batch(sid):
          continue
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
      LOGGER.log(
        f"Stepping to layer {layer.layer_order}: {layer.stamps[0].name},"
        f" stamps: {len(layer.stamps)}, iters {layer.lcp.num_iter}"
      )
      self.schedule_layer_start(layer)
      self.run_layer(layer)

      # Only recompute reload state for replicas that run THIS layer
      for sid, _ in enumerate(self.state.pm_reload):
        if layer.runs_replica(sid):
          self.state.pm_reload[sid] = self.check_pm_reload(sid)

    for sid in overlay.get_stampids():
      self.aie_utls[sid].initialize_stamp()

    LOGGER.log("\nFinished Execution")
    self._handle_fsp()
    self._write_run_summary("SUCCESS")

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

  def _write_run_summary(self, status):
    """
    Record run state to run_summary.json
    """
    rsf = self.args.top_output_dir + "/run_summary.json"
    flags_dict = dataclasses.asdict(self.args.run_flags)
    summary = {"status": status, "run_flags": flags_dict}

    try:
      pathlib.Path(self.args.top_output_dir).mkdir(parents=True, exist_ok=True)
      with open(rsf, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    except (IOError, OSError) as e:
      print(f"Unable to write run summary file. {e}")
