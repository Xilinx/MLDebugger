# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Keep Track of Execution State and Overlay both in interactive and batch mode
"""


class DebugState:
  """
  Keep Track of debug state
  """

  def __init__(self, layers, stampcount) -> None:
    """
    Initialize the DebugState object.

    Args:
      layers (list): The list of layer objects that define the AIE execution steps.
      stampcount (int): The number of stamps.
    """
    self.current_layer = -1
    self.cur_it = 1
    self.ofm_ping = True
    self.layers = layers
    self.manual_breakpoints = []
    # Run AIE to finish without invoking breakpoints
    self.continue_to_finish = False
    self.error = False
    self.break_on_stamp_scheduled = [False for _ in range(stampcount)]
    self.pm_reload = [False for _ in range(stampcount)]

  def update_layer(self):
    """
    Move to the next layer in the list and reset ping state and iteration counter.

    Yields:
      Layer objects in order as the state progresses.
    """
    for y in self.layers:
      self.current_layer += 1
      self.ofm_ping = True
      self.cur_it = 1
      yield y

  def get_next_layer_for_stamp(self, stamp_id, idx=0):
    """
    Find the next layer that includes the specified stamp_id.
    """
    for i in range(self.current_layer + idx, len(self.layers)):
      layer = self.layers[i]
      if stamp_id < len(layer.stamps):
        return layer
    return None

  def get_current_layer(self):
    """
    Get the current layer object corresponding to the execution state.

    Returns:
      The currently active layer object or None if not set.
    """
    if self.current_layer >= 0:
      try:
        return self.layers[self.current_layer]
      except IndexError:
        pass
    return None

  def get_previous_layer(self):
    """
    Get the previous (most recent) layer object from the state.

    Returns:
      The previous layer object, or None if at the first layer.
    """
    if self.current_layer < 1:
      return None
    return self.layers[self.current_layer - 1]

  def get_layer_by_order(self, order):
    """
    Find and return a layer matching the given order index.

    Args:
      order (int): The order value sought (layer_order attribute).

    Returns:
      The matching layer object, or None if not found.
    """
    if order is not None:
      for l in self.layers:
        if l.layer_order == order:
          return l
    return None

  def get_next_layer(self):
    """
    Retrieve the layer object for the next sequential layer.

    Returns:
      The next layer object if it exists, otherwise None.
    """
    if self.current_layer < len(self.layers) - 1:
      return self.layers[self.current_layer + 1]
    return None

  def get_last_layer(self):
    """
    Get the final (last) layer in the execution order.

    Returns:
      The last layer object in the list.
    """
    return self.layers[-1]

  def add_breakpoint(self, layer, iteration):
    """
    Add a (layer, iteration) tuple as a manual breakpoint
    and sort the list of manual breakpoints.

    Args:
      layer (int): Layer index (or handle).
      iteration (int): Iteration number within the layer.
    """
    self.manual_breakpoints.append((layer, iteration))
    self.manual_breakpoints = sorted(self.manual_breakpoints)
