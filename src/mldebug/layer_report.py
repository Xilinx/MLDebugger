# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2026 Advanced Micro Devices, Inc. All rights reserved.

"""
Text report of the debuggable layers in a design.

Renders LayerInfo.layers as one line per layer so a user can see what the
debugger will step through without running it. Kept out of layer_info.py so
the report can grow without touching metadata parsing.

Columns live in _COLUMNS: add, remove or resize an entry there and the header,
separator and every row follow.
"""

from collections import namedtuple

_EMPTY = "-"
_UNKNOWN = "?"
_TRUNC_SUFFIX = "..."
_TRUNC_RESERVE = len(_TRUNC_SUFFIX)
_SEPARATOR_CHAR = "-"
_COLUMN_GAP = " "

_MSG_TITLE = "Debuggable layers: {count}   Overlay: {overlay}"
_MSG_SUBTITLE = (
  "Rows are in execution order; BE_ID is the layer id accepted by breakpoints."
)
_MSG_NO_LAYERS = "(no debuggable layers found)"
_MSG_UNCLAIMED = "{count} mladf layer(s) unmapped: data movement only, nothing to step."
_MSG_UNCLAIMED_COMPUTE = (
  "{count} mladf layer(s) unmapped but running a kernel: mapping gap, "
  "these cannot be stepped or dumped."
)


def _clip(value, width):
  """Fit a cell value into width, marking truncated values with a trailing '...'."""
  text = _EMPTY if value is None or value == "" else str(value)
  return text if len(text) <= width else text[: width - _TRUNC_RESERVE] + _TRUNC_SUFFIX


def _stamp_attr(layer, attr):
  """Attribute of the layer's first stamp, or '' when the layer has none."""
  return getattr(layer.stamps[0], attr) if layer.stamps else ""


def _mladf_id(design_info, layer):
  """mladf layer_id display string for a layer."""
  report = design_info.mladf_report
  if not report or not layer.mladf_ids:
    return _EMPTY
  span = report.format_layer_id_display(layer.mladf_ids)
  return _UNKNOWN if span is None else span


_Column = namedtuple("_Column", "label width align value")

# Width 60 on KERNEL fits most mladf kernel names, whose template arguments are
# what distinguish otherwise identical superkernels.
_COLUMNS = (
  _Column("BE_ID", 5, ">", lambda _di, layer: layer.layer_order),
  _Column("BE_LAYER_NAME", 40, "<", lambda _di, layer: layer.layer_name),
  _Column("KERNEL", 60, "<", lambda _di, layer: _stamp_attr(layer, "name")),
  _Column("#ITERS", 6, ">", lambda _di, layer: layer.lcp.num_iter),
  _Column("#STAMPS", 7, ">", lambda _di, layer: len(layer.stamps)),
  _Column("MLADF_IDS", 13, ">", _mladf_id),
)

_HEADER = _COLUMN_GAP.join(f"{c.label:{c.align}{c.width}}" for c in _COLUMNS)


def _unclaimed_mladf_counts(design_info):
  """(compute, data-movement) counts of mladf layers no buffer_info layer maps to."""
  report = design_info.mladf_report
  if not report:
    return 0, 0
  compute, other = report.get_unclaimed_layers()
  return len(compute), len(other)


def _row(design_info, layer):
  """Format one layer as a table row."""
  cells = []
  for col in _COLUMNS:
    text = _clip(col.value(design_info, layer), col.width)
    cells.append(f"{text:{col.align}{col.width}}")
  return _COLUMN_GAP.join(cells)


def format_layer_report(design_info):
  """Render every debuggable layer as a text table, in execution order."""
  layers = design_info.layers
  lines = []
  design = design_info.format_info()
  if design:
    lines += [design, ""]
  lines += [
    _MSG_TITLE.format(count=len(layers), overlay=design_info.overlay.get_repr()),
    _MSG_SUBTITLE,
    "",
    _HEADER,
    _SEPARATOR_CHAR * len(_HEADER),
  ]
  if not layers:
    lines.append(_MSG_NO_LAYERS)
    return "\n".join(lines)

  for layer in layers:
    lines.append(_row(design_info, layer))

  unclaimed_compute, unclaimed_dm = _unclaimed_mladf_counts(design_info)
  if unclaimed_dm:
    lines.append("")
    lines.append(_MSG_UNCLAIMED.format(count=unclaimed_dm))
  if unclaimed_compute:
    lines.append("")
    lines.append(_MSG_UNCLAIMED_COMPUTE.format(count=unclaimed_compute))
  return "\n".join(lines)
