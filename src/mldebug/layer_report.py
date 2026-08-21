# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2026 Advanced Micro Devices, Inc. All rights reserved.

"""
Text report of the debuggable layers in a design.

Renders LayerInfo.layers as two lines per layer so a user can see what the
debugger will step through without running it. Kept out of layer_info.py so
the report can grow without touching metadata parsing.

Each layer is one record of two lines: every number on the first, then the two
names stacked in one text column:

    BE_ID #ITERS #STAMPS  MLADF_IDS  BE_LAYER_NAME
                                     └ KERNEL

The numbers are fixed-width and left of the names, and neither name is capped,
so a name wider than the terminal costs readability of that name only -- every
number stays lined up and legible.
"""

_EMPTY = "-"
_UNKNOWN = "?"
_SEPARATOR_CHAR = "-"

# Numeric column widths, each sized for its header since those are wider than
# the values in practice. MLADF_IDS also has to fit a span like "60,79-131".
_ID_W = 4
_ITERS_W = 7
_STAMPS_W = 7
_MLADF_W = 7
_GAP = "  "
# Hangs KERNEL under BE_LAYER_NAME so the second line reads as subordinate.
_KERNEL_MARK = "└ "


def _numbers(be_id, iters, stamps, mladf):
  """The four numeric columns that open a layer's first line."""
  return f"{be_id:>{_ID_W}} {iters:>{_ITERS_W}} {stamps:>{_STAMPS_W}} {mladf:>{_MLADF_W}}{_GAP}"


_INDENT = " " * len(_numbers("", "", "", ""))

_HEADER = (
  f"{_numbers('ID', '#ITERS', '#STAMPS', '#MLADF')}LAYER_NAME\n"
  f"{_INDENT}{_KERNEL_MARK}KERNEL"
)

_MSG_TITLE = "Debuggable layers: {count}   Overlay: {overlay}"
_MSG_SUBTITLE = (
  "Two lines per layer, in execution order; ID is the layer id accepted by breakpoints."
)
_MSG_NO_LAYERS = "(no debuggable layers found)"
_MSG_UNCLAIMED = "{count} mladf layer(s) unmapped: data movement only, nothing to step."
_MSG_UNCLAIMED_COMPUTE = (
  "{count} mladf layer(s) unmapped but running a kernel: mapping gap, "
  "these cannot be stepped or dumped."
)


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


def _unclaimed_mladf_counts(design_info):
  """(compute, data-movement) counts of mladf layers no buffer_info layer maps to."""
  report = design_info.mladf_report
  if not report:
    return 0, 0
  compute, other = report.get_unclaimed_layers()
  return len(compute), len(other)


def _record(design_info, layer):
  """The layer's two lines: its numbers and name, then the kernel that runs it."""
  numbers = _numbers(
    layer.layer_order, layer.lcp.num_iter, len(layer.stamps), _mladf_id(design_info, layer)
  )
  return [
    f"{numbers}{layer.layer_name or _EMPTY}",
    f"{_INDENT}{_KERNEL_MARK}{_stamp_attr(layer, 'name') or _EMPTY}",
  ]


def format_layer_report(design_info):
  """Render every debuggable layer as a two-line record, in execution order."""
  layers = design_info.layers
  records = [line for layer in layers for line in _record(design_info, layer)]

  lines = []
  design = design_info.format_info()
  if design:
    lines += [design, ""]
  lines += [
    _MSG_TITLE.format(count=len(layers), overlay=design_info.overlay.get_repr()),
    _MSG_SUBTITLE,
    "",
    _HEADER,
    _SEPARATOR_CHAR * max(len(line) for line in records + _HEADER.split("\n")),
  ]
  if not records:
    lines.append(_MSG_NO_LAYERS)
    return "\n".join(lines)
  lines += records

  unclaimed_compute, unclaimed_dm = _unclaimed_mladf_counts(design_info)
  if unclaimed_dm:
    lines.append("")
    lines.append(_MSG_UNCLAIMED.format(count=unclaimed_dm))
  if unclaimed_compute:
    lines.append("")
    lines.append(_MSG_UNCLAIMED_COMPUTE.format(count=unclaimed_compute))
  return "\n".join(lines)
