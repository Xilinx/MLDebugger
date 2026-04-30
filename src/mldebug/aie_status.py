# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Utility to help make sense of AIE register Values
"""

import re
import json
import subprocess
from typing import Optional

from mldebug.extra.aie_guidance import AIEGuidanceChecker

class AIEStatus:
  """
  Top level class to manage aie status
  """

  def __init__(self, backend, get_debug_tiles, aie_iface, overlay):
    """
    Initialize the AIEStatus manager.

    Parameters:
      backend: Backend object for register access.
      get_debug_tiles: Function to retrieve tiles of interest.
      aie_iface: Interface containing register maps and parsing.
      overlay: Overlay type string, e.g. "1x4x4".
    """
    self.backend = backend
    self.aie_iface = aie_iface
    self.get_debug_tiles = get_debug_tiles
    self.results = {}
    self.overlay = {}
    self.guidance_checker: Optional[AIEGuidanceChecker] = None
    if overlay == "1x4x4":
      self.overlay = self.aie_iface.parse_overlay()

  def _get_bd_meta(self, c, r, mtype, chan_type, chan_num, regdata):
    """
    Retrieve Buffer Descriptor (BD) metadata for a given tile and module type.

    Parameters:
      c: Column index of the tile.
      r: Row index of the tile.
      mtype: Tile/module type identifier.
      regdata: Register data value from which to derive BD metadata.

    Returns:
      String summarizing BD id, length, and address.
    """
    bd_id = self.aie_iface.get_bd_id(regdata)
    bd_info = self.aie_iface.REGS_DMA_BD[mtype]
    bd_0_addr = self.aie_iface.get_bd_base_reg_addr(mtype, chan_type, chan_num, bd_id)
    reg_bd_0 = self.backend.read_register(c, r, bd_0_addr)
    reg_bd_1 = self.backend.read_register(c, r, bd_0_addr + 0x4)
    reg_bd_2 = self.backend.read_register(c, r, bd_0_addr + 0x8)

    bd_len = self.aie_iface.get_bd_length(reg_bd_0, reg_bd_1, reg_bd_2, mtype)
    bd_addr = self.aie_iface.get_bd_address(reg_bd_0, reg_bd_1, reg_bd_2, mtype)
    return f"BD:{bd_id},LEN:{bd_len},ADDR:{hex(bd_addr)}"

  def _append_dma_status(self, mtype, vaiml=False):
    """
    Append DMA channel status info for the given module type.

    Parameters:
      mtype: Module/tile type.
      vaiml: Optional, adds overlay info if True.
    """
    extra_meta = ""
    for ch in ["MM2S", "S2MM"]:
      rtype = f"dma_{ch.lower()}_status"
      regs = list(self.aie_iface.REGS_DMA_STATUS[ch][mtype].items())
      if rtype not in self.results[mtype]:
        self.results[mtype][rtype] = []
      for c, r in self.get_debug_tiles(mtype, raw=True):
        channel = 0
        for name, reg in regs:
          overlay_info = self._add_overlay_info(c, r, mtype, ch.lower(), name)
          regdata = self.backend.read_register(c, r, reg)
          extra_meta = self._get_bd_meta(c, r, mtype, ch, channel, regdata)
          parsed_reg = self.aie_iface.parse_register(name, regdata)
          if overlay_info is None and vaiml:
            continue
          overlay_info = "" if not overlay_info or not vaiml else f" ({overlay_info})"
          self.results[mtype][rtype].append((name + overlay_info, c, r, hex(regdata), extra_meta, parsed_reg))
          channel += 1

  def _append_bd_status(self, mtype, registers):
    """
    Append Buffer Descriptor (BD) status to results for the given module type.

    Parameters:
      mtype: Module/tile type.
      registers: Dictionary of register names and addresses.
    """
    table = "DMA_BD"
    if table not in self.results[mtype]:
      self.results[mtype][table] = []

    # Specify the register sets we are looking for
    rtypes = []
    rsearches = []
    if self.aie_iface.HAS_PER_CHANNEL_BD_REGS[mtype]:
      for ch in ["MM2S", "S2MM"]:
        channels = self.aie_iface.REGS_DMA_BD[mtype][f"{ch.lower()}_base_addr"]
        for num in range(len(channels)):
          rtypes.append(f"DMA_{ch}_{num}_")
          rsearches.append(f"DMA_{ch}_{num}")
    else:
      rtypes.append("DMA_")
      rsearches.append("DMA_BD")

    # Traverse all tiles of this type and all sets within that tile
    for c, r in self.get_debug_tiles(mtype, raw=True):
      for rnum, rtype in enumerate(rtypes):
        rsearch = rsearches[rnum]
        filtered_regs = [(k.split(rtype)[1], v) for k, v in registers.items() if rsearch in k]

        regdata = {}
        for name, reg in filtered_regs:
          regval = hex(self.backend.read_register(c, r, reg))
          name = name.split("_")[0]
          if name not in regdata:
            regdata[name] = []
          regdata[name].append(regval)
        filtered_regdata = {}
        for k, v in regdata.items():
          # Ignore when BD is not used (i.e., all 0s)
          if not all(x == "0x0" for x in v):
            filtered_regdata[k] = v
        regdata = filtered_regdata
        self.results[mtype][table].append((rsearch, c, r, regdata))

  def append_status(self, mtype, rtype, regs, coalesce=False, _hex=True):
    """
    Get and append status for a module type to results.

    Parameters:
      mtype: Module/tile type.
      rtype: Register status type string.
      regs: List or tuple of (register name, register address) pairs.
      coalesce: Save results on a per tile basis if True.
      _hex: Save register values as hex strings if True.
    """
    extra_meta = ""
    if rtype not in self.results[mtype].keys():
      self.results[mtype][rtype] = []
    for c, r in self.get_debug_tiles(mtype, raw=True):
      if not coalesce:
        for name, reg in regs:
          regdata = self.backend.read_register(c, r, reg)
          if "EVENT_STATUS" in name and regdata == 0:
            continue
          parsed_reg = self.aie_iface.parse_register(name, regdata)
          if _hex:
            regdata = hex(regdata)
          self.results[mtype][rtype].append((name, c, r, regdata, extra_meta, parsed_reg))
      else:
        regdata = []
        for name, reg in regs:
          regval = self.backend.read_register(c, r, reg)
          # Ignore zero data
          if regval == 0:
            continue
          if _hex:
            regval = hex(regval)
          regdata.append((name, regval))
        self.results[mtype][rtype].append((rtype, c, r, regdata))

  def _read_ddr_with_devmem(self, address, width=32):
    """
    Read from DDR memory using devmem command.

    Parameters:
      address: Physical address to read from.
      width: Bit width, should be 32 or 64 (default 32).

    Returns:
      Integer value read from memory, or None on error.
    """
    try:
      if width == 64:
        # Read 64-bit by reading two 32-bit values and combining.
        result_low = subprocess.run(
          ["devmem2", hex(address)],
          capture_output=True,
          text=True,
          check=True
        )
        match_low = re.search(r':\s*(0x[0-9a-fA-F]+)', result_low.stdout)
        if not match_low:
          print(f"[WARNING] Failed to parse devmem2 output for address {hex(address)}")
          return None
        low_val = int(match_low.group(1), 16)

        result_high = subprocess.run(
          ["devmem2", hex(address + 4)],
          capture_output=True,
          text=True,
          check=True
        )
        match_high = re.search(r':\s*(0x[0-9a-fA-F]+)', result_high.stdout)
        if not match_high:
          print(f"[WARNING] Failed to parse devmem2 output for address {hex(address + 4)}")
          return None
        high_val = int(match_high.group(1), 16)

        return (high_val << 32) | low_val
      else:
        result = subprocess.run(
          ["devmem2", hex(address)],
          capture_output=True,
          text=True,
          check=True
        )
        match = re.search(r':\s*(0x[0-9a-fA-F]+)', result.stdout)
        if not match:
          print(f"[WARNING] Failed to parse devmem2 output for address {hex(address)}")
          return None
        return int(match.group(1), 16)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
      print(f"[WARNING] Failed to read from DDR address {hex(address)}: {e}")
      return None

  def _append_hsa_queue_status(self):
    """
    Read and append HSA queue status from DDR memory for each microcontroller.
    Only relevant for aie2ps devices.
    Skipped in test mode.
    """
    if self.backend.is_simulation or self.backend.is_offline:
      return

    # Check if HSA queue registers exist (only in aie2ps)
    if "HSA_QUEUE_HIGH_ADDR" not in self.aie_iface.Shim_uc_registers:
      return

    hsa_high_reg = self.aie_iface.Shim_uc_registers["HSA_QUEUE_HIGH_ADDR"]
    hsa_low_reg = self.aie_iface.Shim_uc_registers["HSA_QUEUE_LOW_ADDR"]

    # Add HSA_QUEUE_STATUS to results if UC_STATUS exists
    if "UC_STATUS" in self.results.get(self.aie_iface.SHIM_TILE_T, {}):
      for uc_data in self.results[self.aie_iface.SHIM_TILE_T]["UC_STATUS"]:
        c = uc_data[1]  # column
        r = uc_data[2]  # row

        # Read HSA queue address registers
        hsa_high = self.backend.read_register(c, r, hsa_high_reg)
        hsa_low = self.backend.read_register(c, r, hsa_low_reg)

        # Combine into 64-bit address
        hsa_queue_addr = (hsa_high << 32) | hsa_low

        # Add HSA queue info to UC_STATUS data
        hsa_info = []
        hsa_info.append(("HSA_QUEUE_ADDR", hex(hsa_queue_addr)))

        # Validate address - skip DDR reads for invalid addresses
        # 0x0 indicates no queue, 0xffffffffffffffff indicates uninitialized/invalid
        if hsa_queue_addr != 0 and hsa_queue_addr != 0xffffffffffffffff:
          # Read queue information from DDR
          read_index = self._read_ddr_with_devmem(hsa_queue_addr + 0x0, 64)
          write_index = self._read_ddr_with_devmem(hsa_queue_addr + 0x10, 64)
          queue_capacity = self._read_ddr_with_devmem(hsa_queue_addr + 0xc, 32)

          if read_index is not None:
            hsa_info.append(("HSA_READ_INDEX", read_index))
          if write_index is not None:
            hsa_info.append(("HSA_WRITE_INDEX", write_index))
          if queue_capacity is not None:
            hsa_info.append(("HSA_QUEUE_CAPACITY", queue_capacity))

        # Append HSA info to existing UC data
        uc_data[3].extend(hsa_info)

  def _append_uc_status(self):
    """
    Append microcontroller (UC) status from shim registers to results for SHIM_TILE_T.
    Includes translation of firmware state if present.
    """
    regs = list(self.aie_iface.Shim_uc_registers.items())
    self.append_status(self.aie_iface.SHIM_TILE_T, "UC_STATUS", regs, coalesce=True, _hex=True)
    for uc_data in self.results[self.aie_iface.SHIM_TILE_T]["UC_STATUS"]:
      data = uc_data[3]
      for i, entry in enumerate(data):
        if "FW_STATE" in entry:
          state = self.aie_iface.FIRMWARE_STATES_MAP.get(int(entry[1], base=16))
          if state:
            data[i] = ("FW_STATE", state)
            break

  def _append_core_status(self, debug_mode=False):
    """
    Append core status to results for AIE_TILE_T.

    Parameters:
      debug_mode: If True, add debug control register values.
    """
    mtype = self.aie_iface.AIE_TILE_T
    cs_k = "CORE_STATUS"
    cpc_k = "CORE_PC"

    regmap = self.aie_iface.Core_registers
    if cs_k not in self.results[mtype].keys():
      self.results[mtype][cs_k] = []
    for c, r in self.get_debug_tiles(mtype, raw=True):
      regdata = self.backend.read_register(c, r, regmap[cs_k])
      cs_parsed = self.aie_iface.parse_register(cs_k, regdata)
      cs_val = hex(regdata)

      # PC register is not accessible if core is in reset
      reset_value = 1 << self.aie_iface.core_status_strings.index("Reset")
      cpc_val = "NA" if regdata == reset_value else self.backend.read_register(c, r, regmap[cpc_k])

      if debug_mode:
        dbg_ctrl_1 = hex(self.backend.read_register(c, r, regmap["DEBUG_CONTROL1"]))
        self.results[mtype][cs_k].append((f"DBG_CTRL:{dbg_ctrl_1}", c, r, cs_val, f"PC:{cpc_val}", cs_parsed))
      else:
        cs_parsed += f",PC:{cpc_val}"
        self.results[mtype][cs_k].append((cs_k, c, r, cs_val, "", cs_parsed))

  def _add_overlay_info(self, c, r, mtype, chtype, name):
    """
    Get overlay metadata for a DMA channel: e.g., OFM, IFM, or WTS.

    Parameters:
      c: Tile column index.
      r: Tile row index.
      mtype: Module/tile type.
      chtype: Channel direction ("mm2s" or "s2mm").
      name: Register name string.

    Returns:
      String with overlay info, or "" if unavailable, or None if not found for vaiml filtering.
    """
    if mtype == self.aie_iface.SHIM_TILE_T or (c, r) not in self.overlay:
      return ""
    match = re.search(r"(?:S2MM|MM2S)(\d+)", name)
    if not match:
      return ""
    channel = match.group(1)

    for connection in self.overlay[(c, r)]:
      path = ""
      if mtype != self.aie_iface.AIE_TILE_T:
        connection_str = ""
        if "connections" in connection and connection["connections"]:
          connection_str = f"[{''.join(str(tuple(e)) for e in connection['connections'])}]"
        path = f" {connection['src']} -> {connection['dst']}{connection_str}"
      if connection["channel"] == int(channel) and connection["direction"].lower() == chtype:
        ctype = connection["type"]
        return f"{ctype}{path}"
    return None

  def get_vaiml_status(self, filename=None, tile_type=None, debug_map_json=None, guidance=False):
    """
    Collect and print or save status for all requested tiles, including overlay info.

    Parameters:
      filename: Optional; destination filename to write output.
      tile_type: Optional; list of tile types to include.
      debug_map_json: Optional; path to JSON file for extra debug mapping.
      guidance: Optional; if True, run guidance checks after status collection. Default: True
    """
    self.get(filename, tile_type, True, debug_map_json=debug_map_json, guidance=guidance)

  def _get_advanced_metrics(self, ttype, registers, advanced):
    """
    Append advanced diagnostics to results for core, memory, or shim tiles.

    Parameters:
      ttype: Module/tile type.
      registers: Register dictionary.
      advanced: Boolean flag to append advanced details.
    """
    if not advanced:
      return
    # Append lock values and bd status
    self._append_bd_status(ttype, registers)
    rtype = "LOCK_VALUE"
    # Use lock number for simplified output
    filtered_regs = [(k.split("_")[-1], v) for k, v in registers.items() if rtype in k]
    self.append_status(ttype, rtype, filtered_regs, coalesce=True, _hex=False)

  def print_core_summary(self, guidance=False):
    """
    Prints a summary of the core status for all AIE tiles.

    Parameters:
      guidance: Optional; if True, run guidance checks after status. Default: True
    """
    self.results = {}
    self.results[self.aie_iface.AIE_TILE_T] = {}
    self._append_core_status(debug_mode=True)
    print_status_data(self.results, None)

    # Run guidance checks after status output
    if guidance:
      self.run_guidance_checks(show_passed=False, show_guidance=True)

  def update(self, tile_type=None, vaiml=False, advanced=False, debug_map_json=None):
    """
    Query and store status for all requested tiles.

    Parameters:
      tile_type: Optional; list of tile/module types.
      vaiml: Optional; if True, include vaiml/overlay info.
      advanced: Optional; include extra diagnostics.
      debug_map_json: Optional; debug map path for microcontroller section.
    """
    if not tile_type:
      tile_type = self.aie_iface.TILE_TYPES

    for ttype in tile_type:
      self.results[ttype] = {}
      if ttype == self.aie_iface.AIE_TILE_T:
        self._append_core_status()
        core_metrics = ["EVENT_STATUS", "LOCK_OFL", "LOCK_UFL"]
        if advanced:
          core_metrics.extend(["CORE_SR1", "CORE_SR2"])
        regmap = self.aie_iface.Core_registers
        for rtype in core_metrics:
          regs = [(k, v) for k, v in regmap.items() if rtype in k]
          self.append_status(ttype, rtype, regs)
        self._get_advanced_metrics(ttype, regmap, advanced)
      if ttype == self.aie_iface.MEM_TILE_T:
        self._get_advanced_metrics(ttype, self.aie_iface.Memory_tile_registers, advanced)
      # Shim Microcontroller
      if ttype == self.aie_iface.SHIM_TILE_T:
        regmap = self.aie_iface.Shim_tile_registers
        self._get_advanced_metrics(ttype, regmap, advanced)
        if self.aie_iface.HAS_UC_MODULE:
          self._get_uc_status(debug_map_json=debug_map_json)
      # DMA in AIE, Shim and MEM Tiles
      self._append_dma_status(ttype, vaiml)

  def get(self, filename=None, tile_type=None, vaiml=False, advanced=False, debug_map_json=None, guidance=False):
    """
    Query, store, and print or save status for all requested tiles.

    Parameters:
      filename: Optional; if given, write output to this file.
      tile_type: Optional; restriction on tile types. Default: ['aie_tile', 'mem_tile', 'shim_tile']
      vaiml: Optional; enable vaiml/overlay annotation.
      advanced: Optional; enable advanced diagnostics.
      debug_map_json: Optional; debug file path for uC tiles.
      guidance: Optional; if True, run guidance checks after status collection. Default: True
    """
    self.update(tile_type, vaiml, advanced, debug_map_json=debug_map_json)
    print_status_data(self.results, filename)

    # Run guidance checks after status output
    if guidance:
      self.run_guidance_checks(show_passed=False, show_guidance=True)

  def _get_uc_status(self, debug_map_json=None):
    """
    Collect and enhance microcontroller (uC) status for SHIM_TILE_T tiles.

    Parameters:
      debug_map_json: Optional; JSON file containing debug opcodes/location.
    """
    if self.aie_iface.SHIM_TILE_T not in self.results:
      self.results[self.aie_iface.SHIM_TILE_T] = {}
    self._append_uc_status()
    # Add HSA queue status for aie2ps
    self._append_hsa_queue_status()
    if debug_map_json is not None:
      with open(debug_map_json, "r", encoding="utf-8") as f:
        data = json.load(f)
        entries = data.get("debug", [])
      prev_map = {}
      prev_entry = None
      for entry in entries:
        key = (entry.get("page_offset"), entry.get("column"))
        prev_map[key] = None
        if prev_entry:
          prev_map[key] = prev_entry.get("operation"), prev_entry.get("line"), prev_entry.get("file")
        prev_entry = entry
      for uc_data in self.results[self.aie_iface.SHIM_TILE_T]["UC_STATUS"]:
        d = dict(uc_data[3])
        if "PAGE_INDEX" in d and "OFFSET" in d:
          page_offset = (int(d["PAGE_INDEX"], 16) * 8 * 1024) + 16 + int(d["OFFSET"], 16)
          uc_data[3].insert(0, ("PAGE_OFFSET", page_offset))
          if (page_offset, uc_data[1]) in prev_map:
            entry = prev_map.get((page_offset, uc_data[1]))
            if entry:
              uc_data[3].insert(0, ("ASM_OPCODE_FILE", entry[2]))
              uc_data[3].insert(0, ("ASM_OPCODE_LINE", entry[1]))
              uc_data[3].insert(0, ("ASM_OPCODE", entry[0]))

  def get_uc_status(self, debug_map_json=None, guidance=False):
    """
    Print out microcontroller (uC) status from command line.
    If debug_map_json is provided, include opcode/line mappings.
    NOTE: This takes long time to process if debug_map_json is present.

    Parameters:
      debug_map_json: Optional; debug info file.
      guidance: Optional; if True, run guidance checks after status. Default: True
    """
    self.results = {}
    if self.aie_iface.HAS_UC_MODULE:
      self._get_uc_status(debug_map_json=debug_map_json)
      print_status_data(self.results, None)
      print_status_data(self.results, "uc_status.txt")
      print("[INFO] Microcontroller Status written to uc_status.txt.")

      # Run guidance checks after status output
      if guidance:
        self.run_guidance_checks(show_passed=False, show_guidance=True)
    else:
      print("UC Module is not present in this device.")

  def run_guidance_checks(self, show_passed=False, show_guidance=True,
                          export_json=None):
    """
    Run guidance checks on collected status data and display results.

    Parameters:
      show_passed: If True, show rules that passed in addition to failures.
      show_guidance: If True, show detailed guidance messages for failures.
      export_json: If provided, export results to a JSON file.
    """
    if not self.results:
      print("[WARNING] No status data available. Run update() or get() first.")
      return

    # Initialize guidance checker if not already done
    if self.guidance_checker is None:
      self.guidance_checker = AIEGuidanceChecker(aie_iface=self.aie_iface)

    # Run all guidance checks
    self.guidance_checker.check_all(self.results)

    # Print results
    self.guidance_checker.print_results(show_passed=show_passed, show_guidance=show_guidance)

    # Export to JSON if requested
    if export_json:
      self.guidance_checker.export_json(export_json)


def format_section_data(section_data):
  """
  Helper function to format section data for printing.

  Parameters:
    section_data: Iterable of tuples describing register/tile status.

  Returns:
    String with formatted, human-readable table of the data.
  """
  output = []
  # Creating headers based on the tuple structure
  headers = ["Name", "Col", "Row", "Status Value", "Status Metadata", "Status Interpretation"]
  header_line = " | ".join(headers)
  output.append(header_line + "\n")
  output.append("-" * 80 + "\n")  # Adding a simple separator
  for item in section_data:
    row = " | ".join(str(x) for x in item)
    output.append(row + "\n")
  return "".join(output)


def print_status_data(data, filename):
  """
  Print or write out status results data.

  Parameters:
    data: Results dictionary from AIEStatus.
    filename: Destination file name; prints to stdout if None.
  """
  lines = []
  for main_key, subdict in data.items():
    for sub_key, values in subdict.items():
      # Add section header
      section_header = f"Section: {main_key.upper()}, Sub-section: {sub_key}\n"
      lines.append(section_header)
      lines.append("-" * 80 + "\n")
      # Add formatted data
      formatted_data = format_section_data(values)
      lines.append(formatted_data)
      lines.append("\n")  # Adding a blank line for better separation

  if filename:
    with open(filename, "w", encoding="utf-8") as file:
      file.write("".join(lines))
  else:
    print("".join(lines))
