# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2026 Advanced Micro Devices, Inc. All rights reserved.

"""
Manages overlays and stamps
"""


class Overlay:
  """
  Abstraction for AIE Overlay.

  Layout is BxSxCxR where:
    B = number of batches (data-parallel copies of the design)
    S = number of stamps per batch (spatial replicas inside one batch)
    C = columns per stamp
    R = rows per stamp

  Replicas are packed stamp-inner along columns: replica i = b*S + s occupies
  columns [i*C, (i+1)*C). The flat replica id is what the rest of the system
  refers to as "sid" (stamp id).
  """

  def __init__(self, args, layout):
    """
    Initialize the Overlay with layout and tile information.

    Args:
      args: Argument object containing configuration options, including
        aie_iface and overlay string.
      layout: Tuple representing the layout from buffer_info. Either
        (batches, stamps, nrow, ncol) (new 4-element form) or
        (stamps, nrow, ncol) (legacy; treated as batches=1).
    """
    self.aie_iface = args.aie_iface
    self.stamps = {}
    self.impls = {}
    batches, stamps_per_batch, ncol, nrow = self._get_layout(args.overlay, layout)

    # Materialize tiles for every physical replica so dropped ones stay quiescible.
    for b in range(batches):
      for s in range(stamps_per_batch):
        replica_id = b * stamps_per_batch + s
        tiles = []
        start_col = replica_id * ncol
        for col in range(start_col, start_col + ncol):
          for row in range(nrow + self.aie_iface.AIE_TILE_ROW_OFFSET):
            tiles.append((col, row))
        self.stamps[replica_id] = tiles

    # Without `multistamp`, collapse to one active replica so LayerInfo/DebugState/
    # backends size to it; extras stay in self.stamps (see get_inactive_tiles).
    if args.run_flags.multistamp:
      self.layout = (batches, stamps_per_batch, ncol, nrow)
    else:
      self.layout = (1, 1, ncol, nrow)

  def _get_layout(self, args_overlay, layout):
    """
    Determine the overlay layout parameters as (batches, stamps, ncol, nrow).

    Args:
      args_overlay (str): User-specified overlay string (e.g. '2x4x4' or
        '4x4'). Parsed as N x C x R; treated as batches=1, stamps=N.
      layout (tuple/list): Layout supplied by LayerInfo. Either
        (batches, stamps, nrow, ncol) (new 4-element form) or
        (stamps, nrow, ncol) (legacy).

    Returns:
      tuple: (batches, stamps, ncol, nrow).
    """
    batches, stamps_per_batch, ncol, nrow = (1, 1, 4, 4)
    if args_overlay:
      parsed = [int(x) for x in args_overlay.split("x")]
      if len(parsed) == 3:
        stamps_per_batch, ncol, nrow = parsed
      elif len(parsed) == 2:
        ncol, nrow = parsed
      else:
        print(f"[WARNING] Cannot parse overlay: {args_overlay}.")
    elif layout:
      if len(layout) == 4:
        # New form from buffer_info: [B, S, R, C]
        batches, stamps_per_batch, nrow, ncol = layout
      elif len(layout) == 3:
        # Legacy form: [stamps, R, C]; batches encoded by caller into stamps
        stamps_per_batch, nrow, ncol = layout

    print("[INFO] Using Layout: ", batches, stamps_per_batch, ncol, nrow)

    return batches, stamps_per_batch, ncol, nrow

  def get_first_relative_core_tile(self, stamp_id=0):
    """
    Get the (col, row) tuple for the first AIE core tile in the specified
    replica, adjusting row by the device-specific tile row offset.

    Args:
      stamp_id (int, optional): Replica index to query. Default is 0.

    Returns:
      tuple: (column, row) of the first core tile within the given replica.
    """
    t = self.get_tiles(self.aie_iface.AIE_TILE_T, stamp_id)[0]
    return t[0], t[1] - self.aie_iface.AIE_TILE_ROW_OFFSET

  def get_tiles(self, tile_type=None, stamp_id=0, raw=False):
    """
    Query tile locations for the overlay.

    Args:
      tile_type (str, optional): Tile type identifier for filtering. If None,
        returns all tile positions.
      stamp_id (int, optional): Replica id to filter tiles by. Defaults to 0.
      raw (bool, optional): Return all tiles for all replicas

    Returns:
      list[tuple]: List of (column, row) tile coordinates corresponding to
        requested tiles.
    """
    tile_list = []
    if raw:
      for sid in self.get_stampids():
        tile_list.extend(self.stamps[sid])
    else:
      tile_list = self.stamps[stamp_id]
    if not tile_type:
      return tile_list
    return self.aie_iface.filter_tiles(tile_type, tile_list)

  def get_stampids(self):
    """
    Get a list of the active replica ids. In single-stamp mode: [0];

    Returns:
      list[int]: List of integer replica ids (length = batches * stamps).
    """
    return list(range(self.get_replica_count()))

  def get_inactive_tiles(self):
    """
    Tiles for physical replicas that exist in the design but fall outside the
    active view (every replica beyond replica 0 when multistamp is disabled).

    Returns:
      list[tuple]: (column, row) tiles to be quiesced. Empty in multistamp mode.
    """
    tiles = []
    for sid in range(self.get_replica_count(), len(self.stamps)):
      tiles.extend(self.stamps[sid])
    return tiles

  def get_replica_count(self):
    """
    Total number of replicas in the overlay (batches * stamps_per_batch).
    """
    return self.layout[0] * self.layout[1]

  def get_stampcount(self):
    """
    Total number of replicas (alias for get_replica_count, kept for
    backward compatibility with existing callers).
    """
    return self.get_replica_count()

  def get_batch_count(self):
    """
    Number of batches (B from BxSxCxR).
    """
    return self.layout[0]

  def get_stamps_per_batch(self):
    """
    Number of stamps within a single batch (S from BxSxCxR).
    """
    return self.layout[1]

  def replica_to_batch(self, sid):
    """
    Map a flat replica id to its batch index.
    """
    return sid // self.layout[1]

  def replica_to_stamp(self, sid):
    """
    Map a flat replica id to its per-batch stamp index.
    """
    return sid % self.layout[1]

  def is_leftmost_in_batch(self, sid):
    """
    True if this replica is the leftmost stamp of its batch (per-batch stamp
    index == 0). The leftmost-in-batch replica is always scheduled at every
    layer; the others may skip layers.
    """
    return sid % self.layout[1] == 0

  def get_stampwidth(self):
    """
    Get the width (number of columns) for a single stamp/replica.

    Returns:
      int: The number of columns per replica (C from BxSxCxR).
    """
    return self.layout[2]

  def get_repr(self):
    """
    Return the string representation of the overlay layout (e.g. '2x1x4x4'
    or '1x4x4' when only one batch).

    Returns:
      str: Overlay configuration as a 'B x S x C x R' (or 'S x C x R') string.
    """
    batches, stamps, ncol, nrow = self.layout
    if batches == 1:
      return f"{stamps}x{ncol}x{nrow}"
    return f"{batches}x{stamps}x{ncol}x{nrow}"
