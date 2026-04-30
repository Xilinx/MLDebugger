# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Manages overlays and stamps
"""


class Overlay:
  """
  Abstraction for AIE Overlay.
  NxCxR: Stamps/Batches x Cols x Rows.
  """

  def __init__(self, args, layout):
    """
    Initialize the Overlay with layout and tile information.

    Args:
      args: Argument object containing configuration options, including aie_iface and overlay string.
      layout: Tuple representing (stamps, ncol, nrow) as default or externally supplied layout.
    """
    self.aie_iface = args.aie_iface
    self.stamps = {}
    self.impls = {}
    self.layout = self._get_layout(args.overlay, layout)

    # For larger devices, a 4x4 overlay can be repeated (stamped)
    stamps, ncol, nrow = self.layout
    for stamp_id in range(stamps):
      tiles = []
      start_col = stamp_id * ncol
      for col in range(start_col, start_col + ncol):
        for row in range(nrow + self.aie_iface.AIE_TILE_ROW_OFFSET):
          tiles.append((col, row))
      self.stamps[stamp_id] = tiles

  def _get_layout(self, args_overlay, layout):
    """
    Determine the overlay layout parameters (stamps, columns, rows).

    Args:
      args_overlay (str): User-specified overlay string (e.g. '2x4x4').
      layout (tuple/list): Provided layout as (stamps, ncol, nrow).

    Returns:
      tuple: (stamps, ncol, nrow) representing number of stamps, columns, and rows.
    """
    stamps, ncol, nrow = (1, 4, 4)
    if args_overlay:
      layout = [int(x) for x in args_overlay.split("x")]
      if len(layout) == 3:
        stamps, ncol, nrow = layout
      elif len(layout) == 2:
        ncol, nrow = layout
      else:
        print(f"[WARNING] Cannot parse overlay: {args_overlay}.")
    elif layout:
      # Layout in buffer_info will be reversed
      stamps, nrow, ncol = layout
    print("[INFO] Using Layout: ", stamps, ncol, nrow)
    return stamps, ncol, nrow

  def get_first_relative_core_tile(self, stamp_id=0):
    """
    Get the (col, row) tuple for the first AIE core tile in the specified stamp,
    adjusting row by the device-specific tile row offset.

    Args:
      stamp_id (int, optional): Stamp index to query. Default is 0.

    Returns:
      tuple: (column, row) of the first core tile within the given stamp.
    """
    t = self.get_tiles(self.aie_iface.AIE_TILE_T, stamp_id)[0]
    return t[0], t[1] - self.aie_iface.AIE_TILE_ROW_OFFSET

  def get_tiles(self, tile_type=None, stamp_id=0, raw=False):
    """
    Query tile locations for the overlay.

    Args:
      tile_type (str, optional): Tile type identifier for filtering. If None, returns all tile positions.
      stamp_id (int, optional): Stamp ID to filter tiles by. Defaults to 0.
      raw (bool, optional): If True, return all tile positions for all stamps, unfiltered.

    Returns:
      list[tuple]: List of (column, row) tile coordinates corresponding to requested tiles.
    """
    tile_list = []
    if raw:
      for stamp in self.stamps.values():
        tile_list.extend(stamp)
    else:
      tile_list = self.stamps[stamp_id]
    if not tile_type:
      return tile_list
    return self.aie_iface.filter_tiles(tile_type, tile_list)

  def get_stampids(self):
    """
    Get a list of all configured stamp IDs in the overlay.

    Returns:
      list[int]: List of integer stamp IDs available in the layout.
    """
    return list(self.stamps.keys())

  def get_stampcount(self):
    """
    Return the number of stamps present in the overlay.

    Returns:
      int: The stamp count (N from NxCxR).
    """
    return self.layout[0]

  def get_stampwidth(self):
    """
    Get the width (number of columns) for a single stamp within the overlay.

    Returns:
      int: The number of columns in the overlay (C from NxCxR).
    """
    return self.layout[1]

  def get_repr(self):
    """
    Return the string representation of the overlay layout (e.g., '2x4x4').

    Returns:
      str: Overlay configuration as 'N x C x R' string.
    """
    return "x".join([str(x) for x in self.layout])
