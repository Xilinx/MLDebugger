# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
AIE Function Call Tree Visualizer

Parses Peano assembly LST files and generates a pretty call tree visualization.
No third-party dependencies required.

Usage:
  # As a module
  from calltree import AIECallTree

  # From file path
  tree = AIECallTree.from_file("path/to/file.lst")

  # From string content
  tree = AIECallTree.from_string(lst_content)

  # Print visualizations
  tree.print_calltree()
  tree.print_call_relationships()
  tree.print_summary()

  # Get string output
  calltree_str = tree.get_calltree()
  relationships_str = tree.get_call_relationships()
"""

import re
from dataclasses import dataclass, field


@dataclass
class AIEFunc:
  """Represents a function in the AIE assembly."""
  name: str
  start_pc: int
  end_pc: int = 0
  is_tail_call: bool = False
  calls: list = field(default_factory=list)  # List of (pc, target_addr) tuples
  tail_jump_target: int = 0  # For tail calls, the jump target


@dataclass
class CallNode:
  """Node in the call tree."""
  func_name: str
  pc: int  # PC where call was made
  children: list = field(default_factory=list)
  is_tail_call: bool = False


class AIECallTree:
  """
  AIE Function Call Tree analyzer and visualizer.

  Parses Peano assembly LST files and provides methods to visualize
  function call trees and relationships.
  """

  def __init__(self, lst_content):
    """
    Initialize the call tree from LST file content.

    Args:
        lst_content: String content of the LST file
    """
    self._raw_content = lst_content
    self._functions = {}  # start_pc -> AIEFunc
    self._addr_to_name = {}  # start_pc -> name
    self._parse()

  @classmethod
  def from_file(cls, filepath):
    """
    Create an AIECallTree from a file path.

    Args:
        filepath: Path to the .lst file (string or Path object)

    Returns:
        AIECallTree instance
    """
    with open(filepath, 'r', encoding='utf-8') as f:
      content = f.read()
    return cls(content)

  @classmethod
  def from_string(cls, lst_content):
    """
    Create an AIECallTree from LST content string.

    Args:
        lst_content: String content of the LST file

    Returns:
        AIECallTree instance
    """
    return cls(lst_content)

  def _parse(self):
    """Parse the LST content and extract functions and call information."""
    lines = self._raw_content.split('\n')

    # Pattern for function/label header: "00000000 <function_name>:"
    func_pattern = re.compile(r'^([0-9a-f]+)\s+<([0-9a-zA-Z_\s]+)(.+)>:$')
    # Pattern for instruction with PC: "     hex:      instruction"
    instr_pattern = re.compile(r'^\s*([0-9a-f]+):\s+(.+)$')
    # Pattern for jl (jump and link - function call): jl #0xXXXX
    call_pattern = re.compile(r'\bjl\s+#(0x[0-9a-f]+)')
    # Pattern for j (unconditional jump - potential tail call): j #0xXXXX
    jump_pattern = re.compile(r'\bj\s+#(0x[0-9a-f]+)')

    current_func = None

    for line in lines:
      # Check for function/label header
      m_func = func_pattern.match(line)
      if m_func:
        addr = int(m_func.group(1), 16)
        name = m_func.group(2)

        # Skip internal labels (start with '.')
        if name.startswith('.'):
          continue

        # Save previous function if exists
        if current_func:
          if current_func.end_pc == 0:
            current_func.end_pc = addr - 2
            current_func.is_tail_call = True
          self._functions[current_func.start_pc] = current_func

        # Start new function
        current_func = AIEFunc(name=name, start_pc=addr)
        self._addr_to_name[addr] = name
        continue

      # Check for instructions
      m_instr = instr_pattern.match(line)
      if m_instr and current_func:
        pc = int(m_instr.group(1), 16)
        instr = m_instr.group(2)

        # Check for function call (jl)
        m_call = call_pattern.search(instr)
        if m_call:
          target = int(m_call.group(1), 16)
          current_func.calls.append((pc, target))

        # Check for ret (function end)
        if '\tret' in instr or instr.strip().startswith('ret'):
          if current_func.end_pc == 0:
            current_func.end_pc = pc

        # Check for unconditional jump (potential tail call)
        m_jump = jump_pattern.search(instr)
        if m_jump and '\tjl' not in instr:
          target = int(m_jump.group(1), 16)
          current_func.tail_jump_target = target

    # Save last function
    if current_func:
      self._functions[current_func.start_pc] = current_func

  @property
  def functions(self):
    """Get dictionary of all parsed functions (start_pc -> AIEFunc)."""
    return self._functions

  @property
  def function_names(self):
    """Get list of all function names."""
    return list(self._addr_to_name.values())

  @property
  def function_count(self):
    """Get the number of functions parsed."""
    return len(self._functions)

  def get_function_by_name(self, name):
    """
    Find a function by name (partial match supported).

    Args:
        name: Function name or partial name to search for

    Returns:
        AIEFunc or None if not found
    """
    for addr, func_name in self._addr_to_name.items():
      if name in func_name:
        return self._functions.get(addr)
    return None

  def get_function_by_address(self, addr):
    """
    Get a function by its start address.

    Args:
        addr: Start address of the function

    Returns:
        AIEFunc or None if not found
    """
    return self._functions.get(addr)

  def _build_call_tree(self, root_addr):
    """Build a call tree starting from the given address."""
    visited = set()

    def build_tree(addr, depth=0):
      if addr not in self._functions:
        name = self._addr_to_name.get(addr, f"<unknown@0x{addr:x}>")
        return CallNode(func_name=name, pc=addr)

      if addr in visited:
        func = self._functions[addr]
        return CallNode(func_name=f"{func.name} (recursive)", pc=addr)

      visited.add(addr)
      func = self._functions[addr]
      node = CallNode(func_name=func.name, pc=addr, is_tail_call=func.is_tail_call)

      for _, target_addr in func.calls:
        child = build_tree(target_addr, depth + 1)
        node.children.append(child)

      if func.tail_jump_target and func.tail_jump_target in self._addr_to_name:
        target_name = self._addr_to_name[func.tail_jump_target]
        if not target_name.startswith('.'):
          child = build_tree(func.tail_jump_target, depth + 1)
          child.is_tail_call = True
          node.children.append(child)

      visited.discard(addr)
      return node

    return build_tree(root_addr)

  def _visualize_tree(self, node, prefix="", is_last=True, is_root=True):
    """Generate ASCII visualization of a call tree node."""
    lines = []

    if is_root:
      connector = ""
      new_prefix = ""
    else:
      connector = "└── " if is_last else "├── "
      new_prefix = prefix + ("    " if is_last else "│   ")

    tail_marker = " [tail-call]" if node.is_tail_call else ""
    func_display = f"{node.func_name} (0x{node.pc:x}){tail_marker}"
    lines.append(f"{prefix}{connector}{func_display}")

    for i, child in enumerate(node.children):
      is_child_last = i == len(node.children) - 1
      child_lines = self._visualize_tree(child, new_prefix, is_child_last, is_root=False)
      lines.append(child_lines)

    return "\n".join(lines)

  def _get_root_addresses(self, root_func=None):
    """Determine root function addresses for call tree generation."""
    root_addrs = []

    if root_func:
      for addr, name in self._addr_to_name.items():
        if root_func in name:
          root_addrs.append(addr)
          break
    else:
      # Default: start with __start or _main_init
      for addr, name in self._addr_to_name.items():
        if name in ('__start', '_main_init'):
          root_addrs.append(addr)

      # Also find all superkernel functions
      for addr, name in self._addr_to_name.items():
        if 'superkernel' in name.lower() and addr not in root_addrs:
          root_addrs.append(addr)

    return sorted(root_addrs)

  def get_summary(self):
    """
    Get a summary of all functions.

    Returns:
        str: Formatted summary string
    """
    lines = []
    lines.append("=" * 80)
    lines.append("FUNCTION SUMMARY")
    lines.append("=" * 80)
    lines.append(f"{'Address':<12} {'End PC':<12} {'Calls':<6} {'Name'}")
    lines.append("-" * 80)

    for addr in sorted(self._functions.keys()):
      func = self._functions[addr]
      end_str = f"0x{func.end_pc:x}" if func.end_pc else "N/A"
      tail_str = " [tail]" if func.is_tail_call else ""
      lines.append(f"0x{addr:<10x} {end_str:<12} {len(func.calls):<6} {func.name}{tail_str}")

    lines.append("-" * 80)
    lines.append(f"Total functions: {len(self._functions)}")
    lines.append("")

    return "\n".join(lines)

  def get_calltree(self, root_func=None, include_summary=False):
    """
    Get the call tree visualization as a string.

    Args:
        root_func: Optional function name to start from
        include_summary: Whether to include the function summary

    Returns:
        str: Call tree visualization
    """
    output = []

    if include_summary:
      output.append(self.get_summary())

    root_addrs = self._get_root_addresses(root_func)

    output.append("=" * 80)
    output.append("CALLTREES")
    output.append("=" * 80)

    for root_addr in root_addrs:
      root_name = self._addr_to_name.get(root_addr, f"<0x{root_addr:x}>")
      output.append(f"\n┌─ Call tree for: {root_name}")
      output.append("│")

      tree = self._build_call_tree(root_addr)
      output.append(self._visualize_tree(tree))
      output.append("")

    return "\n".join(output)

  def get_call_relationships(self):
    """
    Get a simple list of call relationships.

    Returns:
        str: Formatted call relationships
    """
    lines = []
    lines.append("=" * 80)
    lines.append("CALL RELATIONSHIPS")
    lines.append("=" * 80)

    for addr in sorted(self._functions.keys()):
      func = self._functions[addr]
      if func.calls or func.tail_jump_target:
        lines.append(f"\n{func.name} (0x{addr:x}):")
        for call_pc, target in func.calls:
          target_name = self._addr_to_name.get(target, f"<unknown@0x{target:x}>")
          lines.append(f"  ├─ calls {target_name} at PC 0x{call_pc:x}")
        if func.tail_jump_target and func.tail_jump_target in self._addr_to_name:
          target_name = self._addr_to_name[func.tail_jump_target]
          if not target_name.startswith('.'):
            lines.append(f"  └─ tail-calls {target_name}")

    return "\n".join(lines)

  def print_summary(self):
    """Print the function summary to stdout."""
    print(self.get_summary())

  def print_calltree(self, root_func=None, include_summary=False):
    """
    Print the call tree visualization to stdout.

    Args:
        root_func: Optional function name to start from
        include_summary: Whether to include the function summary
    """
    print(self.get_calltree(root_func, include_summary))

  def print_call_relationships(self):
    """Print the call relationships to stdout."""
    print(self.get_call_relationships())

  def __str__(self):
    """String representation showing summary info."""
    return f"AIECallTree({self.function_count} functions)"

  def __repr__(self):
    """Repr showing class and function count."""
    return f"AIECallTree(functions={self.function_count})"


# Standalone functions for backward compatibility
def parse_lst_file(filepath):
  """Parse LST file and return functions dict and addr_to_name dict."""
  tree = AIECallTree.from_file(filepath)
  return tree._functions, tree._addr_to_name


def generate_calltree_visualization(filepath, root_func=None):
  """Generate complete call tree visualization from file."""
  tree = AIECallTree.from_file(filepath)
  return tree.get_calltree(root_func)


if __name__ == "__main__":
  import sys

  if len(sys.argv) < 2:
    print("Usage: python calltree.py <lst_file> [root_function]")
    print("\nExample:")
    print("  python calltree.py 4_0.lst")
    print("  python calltree.py 4_0.lst superkernel_add1d")
    print("\nAs a module:")
    print("  from calltree import AIECallTree")
    print("  tree = AIECallTree.from_file('4_0.lst')")
    print("  tree.print_calltree()")
    sys.exit(1)

  lst_file = sys.argv[1]
  root_func_arg = sys.argv[2] if len(sys.argv) > 2 else None

  try:
    _tree = AIECallTree.from_file(lst_file)
    _tree.print_calltree(root_func_arg)
    _tree.print_call_relationships()
  except FileNotFoundError:
    print(f"Error: File '{lst_file}' not found.")
    sys.exit(1)
  except Exception as e:
    print(f"Error parsing file: {e}")
    raise
