# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2026 Advanced Micro Devices, Inc. All rights reserved.

"""
Rebuild a kernel's call tree from the linker map file.

Some superkernels fuse many ops behind a single wrapper that the debugger can
only break on as a whole -- templated-graph (TG) kernels today. The caller
decides which layers are worth breaking down; this module just builds the tree
for whatever kernel entry PC it is handed, so the interactive 'i'/info() command
can show what a layer's kernel is actually made of.

The map file's PM section lists every function with its address range, stack
frame and callees, which is enough to recover the tree without the compiler's
own .calltree file. Only the Synopsys bridge (Chess) layout is parsed today:

    0x000009e0..0x00000b57 (       376 items) : <obj>::<symbol> (Function, Global, .text) ...

                Called functions  : <symbol>
                                    <symbol>

Peano/lld map files use a different layout and yield nothing; add a second
parser here when that flow is needed.
"""

import re
import textwrap
from collections import namedtuple
from dataclasses import dataclass, field

_MEM_SECTION = re.compile(r"^Memory map for memory '(\S+)':")
_ENTRY = re.compile(r"^\s+0x([0-9a-f]+)\.\.0x[0-9a-f]+\s+\(\s*(\d+) items\)\s*:\s*(.+?)\s*$")
_FUNCTION = re.compile(
  r"^(?P<sym>\S+)\s+\(Function,\s*\w+,\s*\.text\S*\)"
  r"(?:\s+\(stack frame size = (?P<stack>\d+)\))?$"
)
_CALLED = re.compile(r"^\s+Called functions\s*:\s*(\S+)\s*$")
_CALLED_MORE = re.compile(r"^\s{8,}(\S+)\s*$")
# Leftover mangling when c++filt cannot demangle: _Z[L]<len><name>, lowercased.
_MANGLED = re.compile(r"^_+z[a-z]*?(\d+)(.+)$")

_PM_SECTION = "PM"
_REPEAT_MARK = " (*)"
_EMPTY = "-"
_TRUNC_SUFFIX = "..."
_LOCATION = "on {locations}"


@dataclass
class MapFunction:
  """One PM function entry of a map file."""

  symbol: str
  start_addr: int
  size: int
  stack: int
  callees: list = field(default_factory=list)


@dataclass
class KernelNode:
  """One function of a kernel, plus its position in that kernel's call tree."""

  func: MapFunction
  name: str
  branch: str
  repeated: bool
  aie_func: object = None


@dataclass
class KernelInfo:
  """A kernel's call tree, and a label for where it runs."""

  location: str
  nodes: list = field(default_factory=list)


def parse_map_functions(map_path):
  """Parse the PM section of a map file into {symbol: MapFunction}."""
  functions = {}
  section = None
  current = None
  in_callees = False
  with open(map_path, encoding="utf-8", errors="replace") as fd:
    for line in fd:
      line = line.rstrip("\n")
      m_section = _MEM_SECTION.match(line)
      if m_section:
        section, current, in_callees = m_section.group(1), None, False
        continue
      m_called = _CALLED.match(line)
      if m_called:
        in_callees = current is not None
        if in_callees:
          current.callees.append(m_called.group(1))
        continue
      m_entry = _ENTRY.match(line)
      if m_entry:
        in_callees = False
        current = _make_function(m_entry) if section == _PM_SECTION else None
        if current:
          functions[current.symbol] = current
        continue
      if in_callees:
        m_more = _CALLED_MORE.match(line)
        if m_more:
          current.callees.append(m_more.group(1))
        else:
          in_callees = False
  return functions


def _make_function(m_entry):
  """Build a MapFunction from an entry match, or None when the entry is not a function."""
  m_func = _FUNCTION.match(m_entry.group(3))
  if not m_func:
    return None
  return MapFunction(
    symbol=m_func.group("sym").rsplit("::", 1)[-1],
    start_addr=int(m_entry.group(1), 16),
    size=int(m_entry.group(2)),
    stack=int(m_func.group("stack") or 0),
  )


def _readable(name):
  """Recover the identifier from a name c++filt left mangled, using its length prefix."""
  m_mangled = _MANGLED.match(name)
  if not m_mangled:
    return name
  return m_mangled.group(2)[: int(m_mangled.group(1))] or name


def _build_tree(root_symbol, functions, demangle):
  """Flatten the call tree under root_symbol into pre-order KernelNodes."""
  nodes = []
  expanded = set()

  def visit(symbol, branch, child_prefix):
    func = functions[symbol]
    repeated = symbol in expanded
    nodes.append(KernelNode(func, _readable(demangle(symbol)), branch, repeated))
    if repeated:
      return
    expanded.add(symbol)
    callees = [c for c in func.callees if c in functions]
    for i, callee in enumerate(callees):
      last = i == len(callees) - 1
      visit(
        callee,
        child_prefix + ("└── " if last else "├── "),
        child_prefix + ("    " if last else "│   "),
      )

  visit(root_symbol, "", "")
  return nodes


def _attach_pcs(nodes, aie_functions):
  """Link each node to its AIEFunction from the work dir's parsed ELF listing."""
  # Keyed on entry PC: names cannot identify a function, since the LST parser
  # records locals under a debug label and template clones demangle alike.
  by_pc = {func.start_pc: func for func in aie_functions}
  for node in nodes:
    node.aie_func = by_pc.get(node.func.start_addr)


def build_kernel_info(map_path, start_pc, aie_functions, demangle, location):
  """Call tree of the kernel entered at start_pc, or None when the map has none there."""
  functions = parse_map_functions(map_path)
  root = next((sym for sym, f in functions.items() if f.start_addr == start_pc), None)
  if not root:
    return None
  nodes = _build_tree(root, functions, demangle)
  _attach_pcs(nodes, aie_functions)
  return KernelInfo(location, nodes)


_Column = namedtuple("_Column", "label width align value")


def _pc(node, attr):
  """PC from the work dir function database, 0 when the function is not in it."""
  return getattr(node.aie_func, attr, 0) if node.aie_func else 0


def _hex(value):
  """Format a PC as hex, or '-' when it is unknown."""
  return f"0x{value:06x}" if value else _EMPTY


def _label(node):
  """Tree prefix plus function name, marking calls whose subtree was already shown."""
  return node.branch + node.name + (_REPEAT_MARK if node.repeated else "")


# FUNCTION is wide enough for a 3-deep tree prefix plus a templated kernel name.
_COLUMNS = (
  _Column("FUNCTION", 56, "<", _label),
  _Column("START_PC", 9, ">", lambda n: _hex(n.func.start_addr)),
  _Column("END_PC", 9, ">", lambda n: _hex(_pc(n, "end_pc"))),
  _Column("LOCK_REL", 9, ">", lambda n: _hex(_pc(n, "final_lock_release_pc"))),
  _Column("SIZE", 6, ">", lambda n: n.func.size),
  _Column("STACK", 6, ">", lambda n: n.func.stack),
)

_HEADER = " ".join(f"{c.label:{c.align}{c.width}}" for c in _COLUMNS)


def _clip(value, width):
  """Fit a cell value into width, marking truncated values with a trailing '...'."""
  text = _EMPTY if value is None or value == "" else str(value)
  if len(text) <= width:
    return text
  return text[: width - len(_TRUNC_SUFFIX)] + _TRUNC_SUFFIX


def _row(node):
  """Format one tree node as a table row."""
  cells = [f"{_clip(c.value(node), c.width):{c.align}{c.width}}" for c in _COLUMNS]
  return " ".join(cells)


def _group_identical(kernels):
  """Group trees that match down to every PC; a layer's stamps usually share one."""
  groups = {}
  for kernel in kernels:
    signature = tuple(
      (n.func.symbol, n.func.start_addr, _pc(n, "end_pc"), _pc(n, "final_lock_release_pc"))
      for n in kernel.nodes
    )
    groups.setdefault(signature, []).append(kernel)
  return list(groups.values())


def format_kernel_info(kernels):
  """Render kernel call trees as text, or '' when there are none."""
  groups = _group_identical(kernels)
  lines = []
  for same in groups:
    lines.append("")
    # A single tree covers every stamp; only say who is who when they diverge.
    if len(groups) > 1:
      lines += textwrap.wrap(
        _LOCATION.format(locations=", ".join(k.location for k in same)),
        width=len(_HEADER),
        subsequent_indent="   ",
      )
    lines += [_HEADER, "-" * len(_HEADER)]
    lines += [_row(node) for node in same[0].nodes]
  return "\n".join(lines)
