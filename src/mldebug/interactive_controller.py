# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Interactive mode execution control for AIE debugging.

Provides layer/iteration stepping, manual breakpoint management, and
continue-to-breakpoint logic.  Delegates core execution to BatchRunner.
"""

import sys

from mldebug.utils import LOGGER


class InteractiveController:
  """
  Controls interactive debugging: stepping through layers and iterations,
  managing manual breakpoints, and continuing to breakpoints.

  Delegates core execution (scheduling, layer runs) to a BatchRunner instance.
  """

  def __init__(self, args, state, design_info, impls, aie_utls, runner):
    """
    Args:
      args: Parsed command-line arguments.
      state: DebugState tracking execution state.
      design_info: LayerInfo with overlay and work directory metadata.
      impls: List of backend implementation instances (shared, mutable).
      aie_utls: List of AIEUtil instances per stamp (shared, mutable).
      runner: BatchRunner for core execution and scheduling.
    """
    self.args = args
    self.state = state
    self.design_info = design_info
    self.impls = impls
    self.aie_utls = aie_utls
    self.runner = runner

  def initialize_aie(self):
    """
    Run initial AIE common setup, advance to layer 0, and schedule startup.

    Returns:
      True if initialization completed.
    """
    self.runner.common_init()
    layer = next(self.state.update_layer())
    self.runner.schedule_layer_start(layer)

    print(f"Initialized State to Layer_{layer.layer_order} Kernel_{layer.stamps[0].name}")
    return True

  def step_iter_manual(self):
    """Step a single iteration manually in console (non-auto) mode."""
    self.step_iteration(False)

  def step_iteration(self, auto_mode):
    """
    Step a single iteration (sets breakpoint at start of next iteration and continues).

    Args:
      auto_mode: Boolean. If True, disables console log message after stepping.

    Returns:
      True if successful, False otherwise or on aie_only mode.
    """
    if self.args.aie_only:
      print("This Functionality is disabled for aie-only debug.")
      return False

    current_layer = self.state.get_current_layer()
    if not current_layer:
      print("[INFO] Unable to step.")
      return True

    if self.state.cur_it == current_layer.lcp.num_iter:
      return self.step_layer()

    self.runner.run_layer(current_layer, target_itr=self.state.cur_it + 1, cur_it=self.state.cur_it)
    self.state.cur_it += 1

    if not auto_mode:
      m = f"Layer:{current_layer.layer_order} {current_layer.stamps[0].name} Itr:{self.state.cur_it - 1}"
      LOGGER.log(f"Stepped from {m} -> {self.state.cur_it}", flush=False)

    last_layer = self.state.get_last_layer()
    if current_layer == last_layer and self.state.cur_it == last_layer.lcp.num_iter:
      self.state.continue_to_finish = True
      print("[INFO] Reached the end of the design.")

    return True

  def step_layer(self):
    """
    Step (advance) to the start of the next layer at the first iteration.

    Returns:
      True if successful, False otherwise or if already at end.
    """
    layer = self.state.get_current_layer()
    if self.args.aie_only or not layer:
      print("[INFO] Unable to step.")
      return False

    cur_it = self.state.cur_it
    self.runner.run_layer(layer, target_itr=layer.lcp.num_iter, cur_it=cur_it)

    for sid in range(len(layer.stamps)):
      self.state.pm_reload[sid] = self.runner.check_pm_reload(sid)

    next(self.state.update_layer())
    next_layer = self.state.get_current_layer()

    if next_layer:
      self.design_info.update_work_dir(next_layer.layer_order)
      self.runner.schedule_layer_start(next_layer)
      m = f"Stepped from Layer:{layer.layer_order} {layer.stamps[0].name} Itr:{cur_it} -> "
      LOGGER.log(m + f"Layer:{next_layer.layer_order} {next_layer.stamps[0].name} Itr:{1}", flush=False)
    else:
      self.state.continue_to_finish = True
      print("[INFO] Reached the end of the design.")
    return True

  def add_breakpoint(self, layer_num, iteration=1):
    """
    Set a breakpoint at the specified layer and/or iteration.

    Args:
      layer_num: Integer layer number.
      iteration: Integer iteration index (default 1).
    """
    if self.args.aie_only:
      print("This Functionality is disabled for aie-only debug.")
      return

    current_layer = self.state.get_current_layer()
    current_layer_order = 0
    if current_layer:
      current_layer_order = current_layer.layer_order
    final_layer_order = self.state.get_last_layer().layer_order
    if layer_num < current_layer_order or layer_num > final_layer_order:
      print(f"[ERROR] Layer Out of bounds. Current: {current_layer_order} Final: {final_layer_order}")
      return

    self.state.add_breakpoint(layer_num, iteration)
    print("Successfully added breakpoint")

  def continue_execution(self):
    """
    Continue execution until the next manual breakpoint or to the end of design.

    Handles Failsafe Partition (FSP) usage for VAIML compilation.
    """
    if self.args.aie_only:
      print("This Functionality is disabled for aie-only debug.")
      return

    is_fsp = self.args.vaiml_folder_path and not self.args.last_fsp
    if self.state.continue_to_finish:
      print("Running AIE to end")
      for sid, impl in enumerate(self.impls):
        impl.continue_aie()
        if is_fsp:
          self.aie_utls[sid].set_fsp_breakpoint()
      if is_fsp:
        print(
          "First, please press Enter ONCE in the VAIML process"
          " to load the next Failsafe Partition, and wait for "
          "`waiting for user input`. Then press q here."
        )
        return
      sys.exit(0)

    if not self.state.get_current_layer():
      print("[INFO] Unable to run further. Continue again to unhalt AIE and finish.")
      self.state.continue_to_finish = True
      return

    if not self.state.manual_breakpoints:
      last_layer = self.state.layers[-1]
      ta_layer = last_layer.layer_order
      ta_itr = last_layer.lcp.num_iter
      print("No Manual breakpoints found.")
      self.state.continue_to_finish = True
    else:
      ta_layer, ta_itr = self.state.manual_breakpoints.pop(0)

    print(f"Goto next breakpoint at layer {ta_layer} iteration {ta_itr}")
    while True:
      cur_layer = self.state.get_current_layer()
      if not cur_layer or not cur_layer.layer_order < ta_layer:
        break
      if not self.step_layer():
        print(f"Unable to continue to breakpoint at layer {ta_layer} iteration {ta_itr}")
        return
    if self.state.continue_to_finish:
      self.step_layer()
    else:
      while self.state.cur_it < ta_itr:
        if not self.step_iteration(True):
          print(f"Unable to continue to breakpoint at layer {ta_layer} iteration {ta_itr}")
          return

    if self.state.continue_to_finish:
      fsp_text = ""
      if is_fsp:
        fsp_text = " or to load the next Failsafe Partition"
      print(
        f"Reached Final iteration : {ta_itr} of layer : {ta_layer}. "
        f"Press c/continue to Run AIE to end and exit{fsp_text}"
      )
    else:
      print()
