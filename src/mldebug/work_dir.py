# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Extract information from AIE Work Directory
"""

import os
import re
import subprocess
from importlib import resources
from dataclasses import dataclass
from pathlib import Path

from mldebug.extra.calltree import AIECallTree
from mldebug.utils import LOGGER, is_aarch64, is_windows

@dataclass
class AIEFunction:
  """
  AIE Kernel
  """

  name: str
  start_pc: int
  end_pc: int
  final_lock_release_pc: int
  tail_call: bool

  def __str__(self):
    """
    Return a pretty string describing this function, including name, start/end PC,
    info on tail calls, and final lock release PC if present.
    """
    s = ""
    if not self.tail_call:
      s += f"{self.name} start: {self.start_pc} end: {self.end_pc} "
    else:
      s += f"{self.name} start: {self.start_pc} tail_call: {self.end_pc} "
    if self.final_lock_release_pc:
      s += f"final_lock_release: {self.final_lock_release_pc}"
    return s


@dataclass
class GlobalVar:
  """
  Global variable in AIE
  """

  name: str
  address: int
  size: int


def _parse_flexml_layer_id(objstr):
  """
  Parse a layer index from an object string using the current BE naming convention.
  Raises:
      RuntimeError: if parsing fails.
  Returns:
      int: The extracted layer id.
  """
  # Try new format: flexml_layer_53 (underscore before number)
  m = re.findall(r"flexml_layer_([0-9]+)", objstr)
  if m:
    return int(m[0])
  # Try old format: flexml_layers[53] (brackets)
  m = re.findall(r"flexml_layers\[([0-9]+)", objstr)
  if m:
    return int(m[0])
  return -1
  #raise RuntimeError(f"Unable to parse flexml layer id from {objstr}")


class WorkDir:
  """
  Abstraction for AIE Work Directory
  """

  def __init__(self, aie_dir, peano, overlay, dump_lst=False):
    """
    Initialize the AIE Work Directory abstraction. Sets up internal state and parses functions.
    Args:
        aie_dir (str): Path to the work directory.
        peano (bool): Whether using peano compiler.
        overlay: Overlay object with get_stampids() and get_first_relative_core_tile().
        dump_lst (bool): Whether to dump LST files.
    """
    num_stamps = len(overlay.get_stampids())

    self.pm_reload_en = [False] * num_stamps
    self.aie_functions = [None] * num_stamps
    self.elf_flxmlid_maps = [None] * num_stamps
    self.globals = [None] * num_stamps
    self.peano = peano
    self.aie_dir = aie_dir
    self.dump_lst = dump_lst

    self._stamp_lst_map = {}
    for sid in range(num_stamps):
      self._stamp_lst_map[sid] = []

    self._initialize_functions(aie_dir, overlay)

  def _demangle(self, fstring):
    """
    Demangle a C++ mangled function name using c++filt.
    Args:
        fstring (str): The mangled function name.
    Returns:
        str: Demangled function name in lowercase, with extraneous symbols stripped.
    """
    exe = "c++filt.exe"
    with resources.as_file(resources.files("mldebug") / "bin" / exe) as objdump_path:
      if is_windows():
        path = str(objdump_path)
      else:
        path = "c++filt"
      fname = subprocess.check_output([path, "-p", fstring]).decode("utf-8")
    return fname.split("\n", maxsplit=1)[0].split("<")[0].lower().strip()

  def _parse_aie_runtime_control(self, work_dir, col, row, stampid):
    """
    Parses 'aie_runtime_control.cpp' to find reloadable sections and layer order
    for a given core (col, row) and stamp ID.

    Args:
        work_dir (str): Path to the work directory.
        col (int): Column of the target core tile.
        row (int): Row of the target core tile.
        stampid (int): Stamp index into overlay.
    Side effects:
        Updates self.elf_flxmlid_maps[stampid] to map ELF partitions to layers.
    """
    elf_layer_map = {}
    # Elfs for different columns can be reloaded in same line so we have to create multiple groups
    pattern = re.compile("reloadable elf for .*{?\\[col:" + f"{col}" + " row:" + f"{row}" + "\\]([0-9]+)(.+)")
    with open(work_dir + "/ps/c_rts/aie_runtime_control.cpp", encoding="utf-8") as fd:
      for line in fd:
        match = pattern.search(line)
        if not match:
          continue
        par = match.group(1)
        elf_layer_map[par] = []
        tokens = match.group(2).split(" ")
        for token in tokens:
          # Support both formats: flexml_layers[N] and flexml_layer_N
          if "flexml_layers" in token or "flexml_layer_" in token:
            layeridx = _parse_flexml_layer_id(token)
            if layeridx == -1:
              continue
            if layeridx in elf_layer_map[par]:
              break
            elf_layer_map[par].append(layeridx)
    self.elf_flxmlid_maps[stampid] = elf_layer_map

  def _get_lst(self, elf_path, elf_name):
    """
    Generate and fetch a disassembly listing (lst) for an ELF file using llvm-objdump.

    Args:
        elf_path (str): Path to the ELF binary.
        elf_name (str): Base ELF file name (stem).

    Returns:
        str: Decoded assembly listing as text.
    Side effects:
        If self.dump_lst is True, writes the output listing to disk.
    """
    lst_data = ""
    exe = "llvm-objdump.elf"
    archname = "aie2p"
    if is_windows():
      exe = "llvm-objdump.exe"
    elif is_aarch64():
      exe = "llvm-objdump.aarch64"
      archname = "aie2ps"
    with resources.as_file(resources.files("mldebug") / "bin" / exe) as objdump_path:
      lst = subprocess.check_output(
        [str(objdump_path), "-d", "-z", "--no-show-raw-insn", f"--arch-name={archname}", "-C", elf_path]
      )
      lst_data = lst.decode("utf-8")

    if self.dump_lst:
      fname = elf_name + ".lst"
      print("Writing assembly listing to:", fname)
      with open(fname, "w", encoding="utf8") as fd:
        fd.write(lst_data)

    return lst_data

  def _get_pc(self, line, llvm=False):
    """
    Extract and return the PC (program counter) value from a disassembly line.

    Args:
        line (str): Line from the disassembly output.
        llvm (bool): If True, expects line format '<addr>: ...'; else Chess style.

    Returns:
        int: Parsed PC value, or 0 on failure.
    """
    if llvm:
      try:
        return int(line.lstrip().split(":")[0], base=16)
      except ValueError:
        return 0
    token = line.lstrip().split(" ")[0]
    if token.isnumeric():
      return int(token)
    return 0

  def _find_next_pc(self, lines, from_line):
    """
    Scan lines, starting after 'from_line', until a line with a PC value is found.

    Args:
        lines (List[str]): Lines from disassembly.
        from_line (int): Index to start searching from.

    Returns:
        (int, int): Tuple of PC value found and the index at which it was found.
    """
    pc = 0
    i = from_line
    while i < (len(lines) - 1):
      i += 1
      pc = self._get_pc(lines[i])
      if pc > 0:
        break
    return (pc, i)

  def _breakpoint_allowed(self, lines, i):
    """
    Check if a breakpoint is allowed at line i by inspecting previous lines
    for NOP or scheduling directives that forbid hardware breakpoints.

    Args:
        lines (List[str]): Disassembly lines.
        i (int): Current line index.

    Returns:
        bool: True if allowed, False if forbidden (with warning).
    """
    for line in [lines[i - 1], lines[i - 2]]:
      if ".nohwbrkpt" in line or ".aggressive_scheduled_block_id" in line:
        LOGGER.verbose_print(f"[WARNING] Breakpoint not allowed at line {i}")
        return False
    return True

  def _initialize_functions(self, work_dir, overlay):
    """
    Parse work directory and its ELF files to extract function ranges, tail calls,
    global variables, and layer/partition info.

    Args:
        work_dir (str): Path to the AIE work directory.
        overlay: Overlay object for tile mapping.
    Side effects:
        Populates aie_functions, pm_reload_en, globals, elf_flxmlid_maps.
    """
    print("[INFO] Try to detect Work Directory ...")
    full_path = Path(work_dir + "/aie/")
    if not Path.exists(full_path):
      LOGGER.log(f"[INFO] Work directory {full_path} does not exist.")
      return
    for stampid in overlay.get_stampids():
      col, row = overlay.get_first_relative_core_tile(stampid)
      core_name = f"{col}_{row}"
      print(f"Core: {core_name}")
      plist = []
      for elf in full_path.glob(f"{core_name}*"):
        plist.append(elf)
      if len(plist) > 1:
        self.pm_reload_en[stampid] = True
        self._parse_aie_runtime_control(work_dir, col, row, stampid)
      self.aie_functions[stampid] = {}

      # Parse LST
      for p in plist:
        LOGGER.verbose_print(f"[INFO] Process: {p}")
        if not self.peano:
          success = self._parse_lst_chess(p, stampid)
          if not success:
            print(f"[WARNING] Failed to parse LST for {p}. Assuming peano compiler.")
            self.peano = True
        if self.peano:
          self._parse_lst_llvm(p, stampid)

      # Parse map file to find LCP
      # Only base map file has global variables
      first_elf = full_path / core_name
      if self.peano:
        self._extract_globals_llvm(first_elf, stampid)
      else:
        self._extract_globals_chess(first_elf, stampid)

  def _parse_lst_chess(self, elf, stampid):
    """
    Extract function boundaries, lock-release PCs, and tail call status from Chess
    LST files for a given ELF and stamp.

    Args:
        elf (Path): Path object for the ELF file directory.
        stampid (int): Stamp index.

    Returns:
        bool: True if parsed successfully, False if the LST file doesn't exist.
    Side effects:
        Populates self.aie_functions[stampid][elf_name] with AIEFunction objects.
    """
    elf_name = elf.stem
    lst_file = f"{elf}/Release/{elf_name}.lst"
    if not Path(lst_file).is_file():
      return False

    self.aie_functions[stampid][elf_name] = []
    with open(lst_file, encoding="utf-8") as fd:
      lines = fd.read().split("\n")
    count = len(lines)
    i = 0
    while i < (count - 1):
      # Found Function Start
      if ".function_start" in lines[i] and ".label" in lines[i - 1]:
        mangled = lines[i - 1].split(" ")[1]
        demangled = self._demangle(mangled)
        # main_init doesn't have a corresponding end PC
        if "_main_init" in demangled or "_main_no_exit_init" in demangled:
          i += 1
          continue
        # Try to look for Start PC for this function
        start_pc, i = self._find_next_pc(lines, i)
        # Try to look for End PC for this function
        end_pc = 0
        last_valid_pc = 0
        final_lock_release_pc = 0
        tail_call = False
        while i < count:
          line = lines[i]
          pc_val = self._get_pc(line)
          if pc_val:
            last_valid_pc = pc_val
          if "REL" in line and self._breakpoint_allowed(lines, i):
            final_lock_release_pc = self._get_pc(line)
          #
          # Following tries to find end of function
          #
          if "RET lr" in line:
            end_pc = self._get_pc(line)
            break  # Function End
          if ".tail_call" in line:
            # LOGGER.verbose_print("tail call found")
            tail_call = True
            end_pc, i = self._find_next_pc(lines, i)
            break  # Assume Function End
          # We found start of a new function so we rollback
          # This can happen when the current function for example uses
          # J statement to go to new function
          if ".function_start" in lines[i] and ".label" in lines[i - 1]:
            end_pc = last_valid_pc
            i -= 1
            break
          i += 1
        self.aie_functions[stampid][elf_name].append(
          AIEFunction(demangled, start_pc, end_pc, final_lock_release_pc, tail_call)
        )
      i += 1
    return True

  def parse_function_sig_llvm(self, fsig):
    """
    Extract the base function name from an LLVM-disassembled function signature.
    Args:
      fsig (str): Function signature string from LLVM
    Returns:
      str: Extracted function base name (without template or argument list).
    """
    # Remove leading 'void' or 'const' if present
    fsig = fsig.lstrip()
    for prefix in ["void", "const"]:
      if fsig.startswith(prefix):
        fsig = fsig[len(prefix) :].lstrip()

    # Determine if function is templated (contains '<' before '(')
    paren_index = fsig.find("(")
    angle_index = fsig.find("<")

    # If no template or parenthesis found before angle bracket
    if paren_index != -1 and (angle_index == -1 or paren_index < angle_index):
      result = fsig.split("(")[0]
    elif angle_index == -1 and angle_index == -1:
      result = fsig
    else:
      # Handle templated function
      if angle_index != -1:
        # Try to extract template part up to the closing '>'
        result = fsig.split(">(")[0]
        if result == fsig:  # No ">(" found
          result = "(".join(fsig.split("(")[0:-1])
        else:
          result += ">"
      else:
        # In case no template or parenthesis is found
        result = "(".join(fsig.split("(")[0:-1])
    return result

  def _extract_globals_llvm(self, elf, sid):
    """
    Extract global variables from the LLVM map file.
    This is used to find the LCP ping/pong variables.

    Args:
        elf (Path): Path object of the ELF directory.
        sid (int): Index into self.globals for this stamp.
    Side effects:
        Appends GlobalVar objects to self.globals[sid] for lcpPing/lcpPong if present.
    """
    mapfile_path = f"{elf}/Release/{elf.stem}.map"
    if not Path(mapfile_path).exists():
      LOGGER.log(f"[WARNING] Map file {mapfile_path} not found. Skipping global extraction.")
      return

    def _extract_var(lines, var_name):
      """
      Find and add a global variable by name from the given lines to self.globals[sid].
      Args:
          lines (List[str]): Lines of map file.
          var_name (str): Variable name to search for.
      Side effects:
          Updates self.globals[sid].
      """
      if not self.globals[sid]:
        self.globals[sid] = []
      for line in lines:
        if var_name in line:
          tokens = line.split()
          if len(tokens) >= 3:
            try:
              self.globals[sid].append(GlobalVar(var_name, int(tokens[0], base=16), int(tokens[2], base=16)))
              LOGGER.verbose_print(f"[INFO] Found global variable: {var_name} at {tokens[0]} size {tokens[2]}")
            except ValueError:
              pass  # Ignore lines that cannot be parsed
            break

    with open(mapfile_path, encoding="utf-8") as fd:
      lines = fd.read().split("\n")
      _extract_var(lines, "lcpPing")
      _extract_var(lines, "lcpPong")

  def _extract_globals_chess(self, elf, sid):
    """
    Extract global variables from the Chess map file.
    This is used to find the LCP ping/pong variables.

    Args:
        elf (Path): Path object of the ELF directory.
        sid (int): Index into self.globals for this stamp.
    Side effects:
        Appends GlobalVar objects to self.globals[sid] for lcpPing/lcpPong if present.
    """
    mapfile_path = f"{elf}/Release/{elf.stem}.map"
    if not Path(mapfile_path).exists():
      LOGGER.log(f"[WARNING] Map file {mapfile_path} not found. Skipping global extraction.")
      return

    def _extract_var(lines, var_name):
      """
      Find and add a global variable by name from the given lines to self.globals[sid].
      Args:
          lines (List[str]): Lines of map file.
          var_name (str): Variable name to search for.
      Side effects:
          Updates self.globals[sid].
      """
      if not self.globals[sid]:
        self.globals[sid] = []
      for line in lines:
        if var_name in line:
          tokens = line.split()[0].split("..")
          if len(tokens) >= 2:
            try:
              start_addr = int(tokens[0], base=16)
              end_addr = int(tokens[1], base=16)
              size = end_addr - start_addr + 1
              self.globals[sid].append(GlobalVar(var_name, start_addr, size))
              LOGGER.verbose_print(f"[INFO] Found global variable: {var_name} at {start_addr} size {size}")
            except ValueError:
              pass  # Ignore lines that cannot be parsed
            break

    with open(mapfile_path, encoding="utf-8") as fd:
      lines = fd.read().split("\n")
      _extract_var(lines, "lcpPing")
      _extract_var(lines, "lcpPong")

  def _parse_lst_llvm(self, elf, stampid):
    """
    Parse LLVM-based LST disassembly to extract functions, boundaries,
    final lock release instructions, and tail call status.

    Args:
        elf (Path): Path object for the ELF file directory.
        stampid (int): Index into aie_functions.
    Side effects:
        Populates self.aie_functions[stampid][elf_name] with AIEFunction objects.
    """
    elf_name = elf.stem
    elf_path = f"{elf}/Release/{elf.stem}"
    data = self._get_lst(elf_path, elf_name)
    self._stamp_lst_map[stampid].append((elf_name, self._get_lst(elf_path, elf_name)))
    lines = data.split("\n")

    self.aie_functions[stampid][elf_name] = []
    flist = self.aie_functions[stampid][elf_name]
    in_func = None
    for i, line in enumerate(lines):
      # function call
      m_fc = re.match(r"([0-9a-f]+) <([^.].+)>", line)
      if m_fc:
        # We reach new function without ret. This implies tail call
        if in_func:
          in_func.end_pc = self._get_pc(lines[i - 2], llvm=True)
          in_func.tail_call = True
          flist.append(in_func)
          in_func = None
        # parse_function_sig_llvm parses templated signature too
        # the command below parses only the first name
        # function_name = m_fc.group(2)#.split("(")[0].split("<")[0].split(" ")[-1]
        function_name = self.parse_function_sig_llvm(m_fc.group(2))
        start_pc = int(m_fc.group(1), base=16)
        in_func = AIEFunction(function_name, start_pc, 0, 0, False)
      # end pc
      elif "ret" in line:
        # functions with multiple returns
        if not in_func:
          flist[-1].end_pc = self._get_pc(line, llvm=True)
        else:
          in_func.end_pc = self._get_pc(line, llvm=True)
          flist.append(in_func)
          in_func = None
      # lock rel
      elif "rel" in line and "rel." not in line:
        # Account for text outside function
        if not in_func:
          continue
        in_func.final_lock_release_pc = self._get_pc(line, llvm=True)

  def find_functions_by_pc(self, pc):
    """
    Given a PC value, return a list of likely candidate function names that cover this PC.
    Searches only in the first stamp's function map.

    Args:
        pc (int): Program counter value to look up.

    Returns:
        List[str]: List of "<elf>:<funcname>" strings whose PC range covers the input.
    """
    funclist = []
    fmap = self.aie_functions[0]
    if fmap:
      for elf, flist in fmap.items():
        for func in flist:
          if func.start_pc <= pc <= func.end_pc:
            funclist.append(f"{elf}:{func.name}")
    return funclist

  def print_aie_functions(self, elf_id=None):
    """
    Print all parsed AIE functions per ELF and/or stamp. If 'elf_id' is supplied,
    limit output to that ELF only.

    Args:
        elf_id (str, optional): Specific ELF base name for function listing.
    Side effects:
        Prints formatted function info to stdout.
    """
    if all(x is None for x in self.aie_functions):
      print("No functions found in design. Please specify aiedir option.")
      return

    sep = "--------------------------------------------"

    if elf_id:
      for fmap in self.aie_functions:
        if elf_id in fmap:
          print(f"{sep}\nFunctions in {elf_id}\n{sep}")
          for f in fmap[elf_id]:
            print(f)
          return

    for stamp, fmap in enumerate(self.aie_functions):
      if not fmap:
        continue
      print(f"{sep}\nElfs in Stamp: {stamp}\n{sep}")
      for elf, flist in fmap.items():
        if elf_id and elf != elf_id:
          continue
        print(f"Functions in ELF {os.path.join(self.aie_dir, elf)}:\n{sep}")
        for f in flist:
          print(f)
        print(sep)

  def print_calltree(self, sid=0):
    """
    Print the call tree for a given stamp.
    Args:
        sid (int): Stamp index.
    """
    if sid not in self._stamp_lst_map:
      LOGGER.log(f"[ERROR] Stamp {sid} not found in _stamp_lst_map.")
      return
    for elf_id, lst_content in self._stamp_lst_map[sid]:
      LOGGER.log(f"[INFO] Printing calltree for {elf_id}\n")
      tree = AIECallTree.from_string(lst_content)
      tree.print_calltree()
      tree.print_call_relationships()
      LOGGER.log(f"[INFO] Done calltree for {elf_id}\n")

  def dump_lst_to_file(self, sid=0):
    """
    Dump the LST file for a given stamp.
    Args:
        sid (int): Stamp index.
    """
    if sid not in self._stamp_lst_map:
      LOGGER.log(f"[ERROR] Stamp {sid} not found in _stamp_lst_map.")
      return
    for elf_id, lst_content in self._stamp_lst_map[sid]:
      with open(f"{elf_id}.lst", "w", encoding="utf-8") as fd:
        fd.write(lst_content)
      LOGGER.log(f"[INFO] LST file dumped to {elf_id}.lst")
