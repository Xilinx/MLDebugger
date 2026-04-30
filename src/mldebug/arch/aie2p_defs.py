# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
AIE2/AIE2P Specific Defs
"""

import json

AIE_TILE_T = "aie_tile"
SHIM_TILE_T = "shim_tile"
MEM_TILE_T = "mem_tile"
TILE_TYPES = [AIE_TILE_T, SHIM_TILE_T, MEM_TILE_T]

AIE_TILE_ROW_OFFSET = 2
MEM_TILE_SZ = 0x80000
HAS_UC_MODULE = False
HAS_PER_CHANNEL_BD_REGS = {AIE_TILE_T: False, SHIM_TILE_T: False, MEM_TILE_T: False}

"""
In logical space (from AIE core's perspective), program memory is at 0x0, while data memory starts at 0x40000.
However, in physical memory data memory starts at 0x0 and program memory starts at 0x20000. Furthermore, data memory
is spread across four memory modules in aie tiles in four directions, but is visible as one contiguous memory for AIE core.
This means, while access data memory in physical space, we need to determine which direction the data memory range falls into.
0 - 0xffff	South 	row - 1
0x10000 - 0x1ffff	West 	col - 1
0x20000 - 0x2ffff	North 	row + 1
0x30000 - 0x3ffff	East 	same tile
For now we assume that we're reading same tile and hence we mask all the logical bits.
"""
LOGICAL_DM_MASK = 0xFFFF

REGS_DMA_BD = {
  MEM_TILE_T: {"num_bd": 48, "base_addr": 0xA0000, "unused": 0, "bd_regcount": 8},
  SHIM_TILE_T: {"num_bd": 16, "base_addr": 0x1D000, "unused": 0, "bd_regcount": 8},
  AIE_TILE_T: {"num_bd": 16, "base_addr": 0x1D000, "unused": 2, "bd_regcount": 8},
}


def get_bd_id(regdata):
  return regdata >> 24 & 0x3F


def get_bd_length(reg_bd_0, reg_bd_1, reg_bd_2, mtype):
  bd_len = 0
  if mtype == AIE_TILE_T:
    bd_len = reg_bd_0 & 0x3FFF
  elif mtype == MEM_TILE_T:
    bd_len = reg_bd_0 & 0x1FFFF
  elif mtype == SHIM_TILE_T:
    bd_len = reg_bd_0
  return bd_len


def get_bd_address(reg_bd_0, reg_bd_1, reg_bd_2, mtype):
  bd_addr = 0
  if mtype == AIE_TILE_T:
    bd_addr = reg_bd_0 >> 14 & 0x3FFF
  elif mtype == MEM_TILE_T:
    bd_addr = reg_bd_1 & 0x7FFFF
  elif mtype == SHIM_TILE_T:
    bd_addr = ((reg_bd_2 & 0xFFFF) << 32) + reg_bd_1
  return bd_addr


def get_bd_base_reg_addr(mtype, chan_type, chan_num, bd_id):
  bd_info = REGS_DMA_BD[mtype]
  return bd_info["base_addr"] + (bd_info["bd_regcount"] * bd_id * 0x4)


def _create_bds(tile_t, registers):
  bd_data = REGS_DMA_BD[tile_t]

  num_bd = bd_data["num_bd"]
  base_addr = bd_data["base_addr"]
  bd_regcount = bd_data["bd_regcount"]
  unused = bd_data["unused"]

  current_addr = base_addr
  for bd in range(num_bd):
    for reg in range(bd_regcount):
      if reg < bd_regcount - unused:
        registers[f"DMA_BD{bd}_{reg}"] = current_addr
      current_addr += 4


Memory_tile_registers = {
  "SPARE_REG": 0x96000,
  "PERF_CNTR_0_CTRL": 0x91000,
  "PERF_CNTR_1_CTRL": 0x91004,
  "PERF_CNTR_0": 0x91020,
  "PERF_CNTR_1": 0x91024,
  "PERF_CNTR_2": 0x91028,
  "PERF_CNTR_3": 0x9102C,
}

_create_bds(MEM_TILE_T, Memory_tile_registers)
for _regid in range(0, 64):
  Memory_tile_registers[f"LOCK_VALUE_{_regid}"] = 0x000C0000 + 0x10 * _regid

Core_registers = {
  "CORE_STATUS": 0x32004,
  "DEBUG_CONTROL0": 0x32010,
  "DEBUG_CONTROL1": 0x32014,
  "EVENT_STATUS0": 0x34200,
  "EVENT_STATUS1": 0x34204,
  "EVENT_STATUS2": 0x34208,
  "EVENT_STATUS3": 0x3420C,
  "EDGE_EVENT_CTRL": 0x34408,
  "PERF_CNTR_0_CTRL": 0x31500,
  "PERF_CNTR_1_CTRL": 0x31500,
  "PERF_CNTR_0": 0x31520,
  "PERF_CNTR_1": 0x31524,
  "PERF_CNTR_2": 0x31528,
  "PERF_CNTR_3": 0x3152C,
  "PERF_CNTR_1_EVENT_VAL": 0x31584,
  "LOCK_OFL": 0x1F120,
  "LOCK_UFL": 0x1F128,
  "CORE_PC": 0x30F00,
  "PC_EVENT0": 0x32020,
  "COMBO_EVENT_INPUTS_A_D": 0x34400,
  "CORE_SR1": 0x30FA0,
  "CORE_SR2": 0x30FB0,
}


def init(is_aie2):
  """
  Inititalize aie2/2p specific
  """
  if is_aie2:
    Core_registers["CORE_PC"] = 0x31100


_create_bds(AIE_TILE_T, Core_registers)
for _regid in range(0, 16):
  Core_registers[f"LOCK_VALUE_{_regid}"] = 0x1F000 + 0x10 * _regid

Shim_tile_registers = {
  "PERF_CNTR_0_CTRL": 0x31000,
  "PERF_CNTR_1_CTRL": 0x31008,
  "PERF_CNTR_0": 0x31020,
  "PERF_CNTR_1": 0x31024,
}
_create_bds(SHIM_TILE_T, Shim_tile_registers)
for _regid in range(0, 16):
  Shim_tile_registers[f"LOCK_VALUE_{_regid}"] = 0x00014000 + 0x10 * _regid

REGS_DMA_STATUS = {
  "S2MM": {
    MEM_TILE_T: {
      "MEM_TILE_S2MM0_STATUS": 0xA0660,
      "MEM_TILE_S2MM1_STATUS": 0xA0664,
      "MEM_TILE_S2MM2_STATUS": 0xA0668,
      "MEM_TILE_S2MM3_STATUS": 0xA066C,
      "MEM_TILE_S2MM4_STATUS": 0xA0670,
      "MEM_TILE_S2MM5_STATUS": 0xA0674,
    },
    SHIM_TILE_T: {"SHIM_S2MM0_STATUS": 0x1D220, "SHIM_S2MM1_STATUS": 0x1D224},
    AIE_TILE_T: {"DMA_S2MM0_STATUS": 0x1DF00, "DMA_S2MM1_STATUS": 0x1DF04},
  },
  "MM2S": {
    MEM_TILE_T: {
      "MEM_TILE_MM2S0_STATUS": 0xA0680,
      "MEM_TILE_MM2S1_STATUS": 0xA0684,
      "MEM_TILE_MM2S2_STATUS": 0xA0688,
      "MEM_TILE_MM2S3_STATUS": 0xA068C,
      "MEM_TILE_MM2S4_STATUS": 0xA0690,
      "MEM_TILE_MM2S5_STATUS": 0xA0694,
    },
    SHIM_TILE_T: {
      "SHIM_MM2S0_STATUS": 0x1D228,
      "SHIM_MM2S1_STATUS": 0x1D22C,
    },
    AIE_TILE_T: {"DMA_MM2S0_STATUS": 0x1DF10, "DMA_MM2S1_STATUS": 0x1DF14},
  },
}

core_status_strings = [
  "Enable",
  "Reset",
  "Memory_Stall_S",
  "Memory_Stall_W",
  "Memory_Stall_N",
  "Memory_Stall_E",
  "Lock_Stall_S",
  "Lock_Stall_W",
  "Lock_Stall_N",
  "Lock_Stall_E",
  "Stream_Stall_SS0",
  "",  # unused bit
  "Stream_Stall_MS0",
  "",  # unused bit
  "Cascade_Stall_SCD",
  "Cascade_Stall_MCD",
  "Debug_Halt",
  "ECC_Error_Stall",
  "ECC_Scrubbing_Stall",
  # The string below defines Error condition where AIE Cores are halted due to an error in operation
  # Look at core events to get more information about the cause
  "Error_Halt",
  "Core_Done",
  "Core_Processor_Bus_Stall",
]

# Events 48-72
error_event_ids = list(range(48, 73))

# Events strings below are used to track which events happened over the course of
# Execution of the design. These are implemented using sticky bits, so if an event happens
# even for one clock cycles, we will see that in the core event list in aie status
core_event_strings = [
  "NONE_CORE",
  "TRUE_CORE",
  "GROUP_0_CORE",
  "TIMER_SYNC_CORE",
  "TIMER_VALUE_REACHED_CORE",
  "PERF_CNT_0_CORE",
  "PERF_CNT_1_CORE",
  "PERF_CNT_2_CORE",
  "PERF_CNT_3_CORE",
  "COMBO_EVENT_0_CORE",
  "COMBO_EVENT_1_CORE",
  "COMBO_EVENT_2_CORE",
  "COMBO_EVENT_3_CORE",
  "EDGE_DETECTION_EVENT_0_CORE",
  "EDGE_DETECTION_EVENT_1_CORE",
  "GROUP_PC_EVENT_CORE",
  "PC_0_CORE",
  "PC_1_CORE",
  "PC_2_CORE",
  "PC_3_CORE",
  "PC_RANGE_0_1_CORE",
  "PC_RANGE_2_3_CORE",
  "GROUP_CORE_STALL_CORE",
  "MEMORY_STALL_CORE",
  "STREAM_STALL_CORE",
  "CASCADE_STALL_CORE",
  "LOCK_STALL_CORE",
  # Event String below indicates Core halt event happened due to debug operation
  "DEBUG_HALTED_CORE",
  # Event String below indicates an active Core
  "ACTIVE_CORE",
  # Event String below indicates a disabled Core
  "DISABLED_CORE",
  "ECC_ERROR_STALL_CORE",
  "ECC_SCRUBBING_STALL_CORE",
  "GROUP_CORE_PROGRAM_FLOW_CORE",
  "INSTR_EVENT_0_CORE",
  "INSTR_EVENT_1_CORE",
  "INSTR_CALL_CORE",
  "INSTR_RETURN_CORE",
  "INSTR_VECTOR_CORE",
  "INSTR_LOAD_CORE",
  "INSTR_STORE_CORE",
  "INSTR_STREAM_GET_CORE",
  "INSTR_STREAM_PUT_CORE",
  "INSTR_CASCADE_GET_CORE",
  "INSTR_CASCADE_PUT_CORE",
  "INSTR_LOCK_ACQUIRE_REQ_CORE",
  "INSTR_LOCK_RELEASE_REQ_CORE",
  "GROUP_ERRORS_0_CORE",
  "GROUP_ERRORS_1_CORE",
  # Event String below indicates Error due to overflow in "Shift Round Saturate" operation
  "SRS_OVERFLOW_CORE",
  # Event String below indicates Error due to overflow in "upshift" operation
  "UPS_OVERFLOW_CORE",
  # Event String below indicates Error due to Floating point overflow
  "FP_HUGE/OVERFLOW_CORE",
  # Event String below indicates Error due to Floating point underflow
  "FP_ZERO/UNDERFLOW_CORE",
  # Event String below indicates Error due to invalid Floating point operation
  "FP_INVALID_CORE",
  # Event String below indicates Error due to Floating point divide by zero error
  "FP_DIV_BY_ZERO_CORE",
  "RESERVED_CORE",
  "PM_REG_ACCESS_FAILURE_CORE",
  "STREAM_PKT_PARITY_ERROR_CORE",
  "CONTROL_PKT_ERROR_CORE",
  # Event String below indicates error while accessing aie core address on AXI control bus
  "AXI_MM_SLAVE_ERROR_CORE",
  # Event String below indicates error due to instruction decompression
  "INSTR_DECOMPRSN_ERROR_CORE",
  # Event String below indicates AIE core hang due to out of range memory access by kernel.
  # This could lead to cores going to ERROR_HALT
  "DM_ADDRESS_OUT_OF_RANGE_CORE",
  "PM_ECC_ERROR_SCRUB_CORRECTED_CORE",
  "PM_ECC_ERROR_SCRUB_2BIT_CORE",
  "PM_ECC_ERROR_1BIT_CORE",
  "PM_ECC_ERROR_2BIT_CORE",
  # Event String below indicates AIE core hang due to out of range Program Memory access
  # This could lead to cores going to ERROR_HALT
  "PM_ADDRESS_OUT_OF_RANGE_CORE",
  # Event String below indicates AIE core hang due to out of range Data memory bank acess
  # This could lead to cores going to ERROR_HALT
  "DM_ACCESS_TO_UNAVAILABLE_CORE",
  # Event String below indicates AIE core hang due to Lock access request to unavailable bank
  # This could lead to cores going to ERROR_HALT
  "LOCK_ACCESS_TO_UNAVAILABLE_CORE",
  "INSTR_WARNING_CORE",
  "INSTR_ERROR_CORE",
  "SPARSITY_OVERFLOW_CORE",
  "STREAM_SWITCH_PORT_PARITY_ERROR_CORE",
  "PROCESSOR_BUS_ERROR_CORE",
  "GROUP_STREAM_SWITCH_CORE",
  "PORT_IDLE_0_CORE",
  "PORT_RUNNING_0_CORE",
  "PORT_STALLED_0_CORE",
  "PORT_TLAST_0_CORE",
  "PORT_IDLE_1_CORE",
  "PORT_RUNNING_1_CORE",
  "PORT_STALLED_1_CORE",
  "PORT_TLAST_1_CORE",
  "PORT_IDLE_2_CORE",
  "PORT_RUNNING_2_CORE",
  "PORT_STALLED_2_CORE",
  "PORT_TLAST_2_CORE",
  "PORT_IDLE_3_CORE",
  "PORT_RUNNING_3_CORE",
  "PORT_STALLED_3_CORE",
  "PORT_TLAST_3_CORE",
  "PORT_IDLE_4_CORE",
  "PORT_RUNNING_4_CORE",
  "PORT_STALLED_4_CORE",
  "PORT_TLAST_4_CORE",
  "PORT_IDLE_5_CORE",
  "PORT_RUNNING_5_CORE",
  "PORT_STALLED_5_CORE",
  "PORT_TLAST_5_CORE",
  "PORT_IDLE_6_CORE",
  "PORT_RUNNING_6_CORE",
  "PORT_STALLED_6_CORE",
  "PORT_TLAST_6_CORE",
  "PORT_IDLE_7_CORE",
  "PORT_RUNNING_7_CORE",
  "PORT_STALLED_7_CORE",
  "PORT_TLAST_7_CORE",
  "GROUP_BROADCAST_CORE",
  "BROADCAST_0_CORE",
  "BROADCAST_1_CORE",
  "BROADCAST_2_CORE",
  "BROADCAST_3_CORE",
  "BROADCAST_4_CORE",
  "BROADCAST_5_CORE",
  "BROADCAST_6_CORE",
  "BROADCAST_7_CORE",
  "BROADCAST_8_CORE",
  "BROADCAST_9_CORE",
  "BROADCAST_10_CORE",
  "BROADCAST_11_CORE",
  "BROADCAST_12_CORE",
  "BROADCAST_13_CORE",
  "BROADCAST_14_CORE",
  "BROADCAST_15_CORE",
  "GROUP_USER_EVENT_CORE",
  "USER_EVENT_0_CORE",
  "USER_EVENT_1_CORE",
  "USER_EVENT_2_CORE",
  "USER_EVENT_3_CORE",
]

# One or more of following errors could lead to hangs or data mismatches
ERRORS_EVENT_REG = "EVENT_STATUS1"
errors_event_strings = [
  "FP_HUGE/OVERFLOW_CORE",
  "FP_ZERO/UNDERFLOW_CORE",
  "FP_INVALID_CORE",
  "FP_DIV_BY_ZERO_CORE",
  "DM_ADDRESS_OUT_OF_RANGE_CORE",
]


def parse_core_status(data):
  """
  Core status register
  """
  output_str = ""
  # print(f"Core Status : {hex(data)} ", end="")
  for s, msg in enumerate(core_status_strings):
    if data >> s & 0x1:
      output_str += msg + ","
  return output_str


def parse_mm2s_status(data):
  """
  MM2S status register
  """
  status = data & 0x3
  output_str = ""
  if status == 0:
    output_str += "IDLE,"
  elif status == 1:
    output_str += "STARTING,"
  else:
    output_str += "RUNNING,"
  if data >> 2 & 1:
    output_str += "Stalled_Lock_Acq,"
  if data >> 3 & 1:
    output_str += "Stalled_Lock_Rel,"
  if data >> 4 & 1:
    output_str += "Stalled_Stream_Backpressure,"
  if data >> 5 & 1:
    output_str += "Stalled_TCT,"
  if data >> 8 & 1:
    output_str += "Error_Lock_Access_to_Unavailable,"
  if data >> 9 & 1:
    output_str += "Error_DM_Access_to_Unavailable,"
  if data >> 10 & 1:
    output_str += "Error_BD_Unavailable,"
  if data >> 11 & 1:
    output_str += "Error_BD_Invalid,"
  if data >> 18 & 1:
    output_str += "Task_Queue_Overflow,"
  return output_str


def parse_s2mm_status(data):
  """
  S2MM status register
  """
  status = data & 0x3
  output_str = ""
  if status == 0:
    output_str += "IDLE,"
  elif status == 1:
    output_str += "STARTING,"
  else:
    output_str += "RUNNING,"
  if data >> 2 & 1:
    output_str += "Stalled_Lock_Acq,"
  if data >> 3 & 1:
    output_str += "Stalled_Lock_Rel,"
  if data >> 4 & 1:
    output_str += "Stalled_Stream_Starvation,"
  if data >> 5 & 1:
    output_str += "Stalled_TCT_or_Count_FIFO_Full,"
  if data >> 8 & 1:
    output_str += "Error_Lock_Access_to_Unavailable,"
  if data >> 9 & 1:
    output_str += "Error_DM_Access_to_Unavailable,"
  if data >> 10 & 1:
    output_str += "Error_BD_Unavailable,"
  if data >> 11 & 1:
    output_str += "Error_BD_Invalid,"
  if data >> 12 & 1:
    output_str += "Error_FoT_Length_Exceeded,"
  if data >> 13 & 1:
    output_str += "Error_FoT_BDs_per_Task,"
  if data >> 18 & 1:
    output_str += "Task_Queue_Overflow,"
  return output_str


def parse_core_events(data, event_id):
  """
  Print core events
  """
  str_id = event_id * 32
  j = 0
  output_str = "\n" + "-" * 40 + "\n"
  for i in range(str_id, str_id + 32):
    if data >> j & 0x1:
      # output_str += f"Bit {j} Event {i} : {core_event_strings[i]}\n"
      output_str += f"({i} : {core_event_strings[i]}) "
    j += 1
  return output_str + "\n"


def parse_core_sr1(data):
  """
  Status register SR1
  """
  output_str = ""
  if data & 1:
    output_str += "Carry,"
  if data >> 1 & 1:
    output_str += "SS0_Success,"
  if data >> 2 & 1:
    output_str += "SS0_Tlast,"
  if data >> 3 & 1:
    output_str += "MS0 success,"
  if data >> 4 & 1:
    output_str += "SRS_Overflow,"
  if data >> 5 & 1:
    output_str += "UPS_Overflow,"
  if data >> 7 & 1:
    output_str += "Float_MAC_Zero_Flag,"
  if data >> 8 & 1:
    output_str += "Float_MAC_Infinity_Flag,"
  if data >> 9 & 1:
    output_str += "Float_MAC_Invalid_Flag,"
  if data >> 10 & 1:
    output_str += "Float_MAC_Tiny_Flag,"
  if data >> 11 & 1:
    output_str += "Float_MAC_Huge_Flag,"
  if data >> 12 & 1:
    output_str += "Bfloat_to_Int_Zero_Flag,"
  if data >> 14 & 1:
    output_str += "Bfloat_to_Int_Invalid_Flag,"
  if data >> 15 & 1:
    output_str += "Bfloat_to_Int_Tiny_Flag,"
  if data >> 16 & 1:
    output_str += "Bfloat_to_Int_Huge_Flag,"
  if data >> 17 & 1:
    output_str += "Float_to_Bfloat_Zero_Flag,"
  if data >> 18 & 1:
    output_str += "Float_to_Bfloat_Infinity_Flag,"
  if data >> 19 & 1:
    output_str += "Float_to_Bfloat_Invalid_Flag,"
  if data >> 20 & 1:
    output_str += "Float_to_Bfloat_Tiny_Flag,"
  if data >> 21 & 1:
    output_str += "Float_to_Bfloat_Huge_Flag,"
  if data >> 22 & 1:
    output_str += "Sparse_Overflow,"
  if data >> 23 & 1:
    output_str += "Fifo_Overflow,"
  if data >> 24 & 1:
    output_str += "Fifo_Underflow,"
  if data >> 29 & 1:
    output_str += "Float_to_Bfp_Invalid_Flag,"
  return output_str


def parse_core_sr2(data):
  """
  Status register SR2
  """
  output_str = ""
  if data & 1:
    output_str += "Float_to_Fix_Zero_Flag,"
  if data >> 2 & 1:
    output_str += "Float_to_Fix_Invalid_Flag,"
  if data >> 5 & 1:
    output_str += "Float_to_Fix_Inexact_Flag,"
  if data >> 6 & 1:
    output_str += "Float_to_Fix_Huge_Flag,"
  if data >> 8 & 1:
    output_str += "Fix_to_Float_Zero_Flag,"
  if data >> 13 & 1:
    output_str += "Fix_to_Float_Inexact_Flag,"
  if data >> 16 & 1:
    output_str += "Nlf_Zero_Flag,"
  if data >> 17 & 1:
    output_str += "Nlf_Infinity_Flag,"
  if data >> 18 & 1:
    output_str += "Nlf_Invalid_Flag,"
  if data >> 19 & 1:
    output_str += "Nlf_Tiny_Flag,"
  if data >> 20 & 1:
    output_str += "Nlf_Huge_Flag,"
  if data >> 21 & 1:
    output_str += "Nlf_Inexact_Flag,"
  return output_str


def parse_core_events0(data):
  """
  parse event0 register
  """
  return parse_core_events(data, 0)


def parse_core_events1(data):
  """
  parse event1 register
  """
  return parse_core_events(data, 1)


def parse_core_events2(data):
  """
  parse event2 register
  """
  return parse_core_events(data, 2)


def parse_core_events3(data):
  """
  parse event3 register
  """
  return parse_core_events(data, 3)


def parse_lock_ofl_ufl(data):
  """
  Parse a lock overflow or underflow register and return a status string
  """
  locks = []
  for x in range(0, 16):
    if data >> x & 0x1:
      locks.append(x)
  return locks


# One time creation of register parsing dict
REG_PARSERS = {
  "CORE_STATUS": parse_core_status,
  "EVENT_STATUS0": parse_core_events0,
  "EVENT_STATUS1": parse_core_events1,
  "EVENT_STATUS2": parse_core_events2,
  "EVENT_STATUS3": parse_core_events3,
  "LOCK_UFL": parse_lock_ofl_ufl,
  "LOCK_OFL": parse_lock_ofl_ufl,
  "CORE_SR1": parse_core_sr1,
  "CORE_SR2": parse_core_sr2,
}
for _t in TILE_TYPES:
  for _k in REGS_DMA_STATUS["S2MM"][_t]:
    REG_PARSERS[_k] = parse_s2mm_status
  for _k in REGS_DMA_STATUS["MM2S"][_t]:
    REG_PARSERS[_k] = parse_mm2s_status


def parse_register(name, data):
  """
  Parse supported registers to string
  """
  if name in REG_PARSERS:
    return REG_PARSERS[name](data)
  return (name, data)


def filter_tiles(tile_type, tile_list):
  """
  Filter a list of tiles based on a given metric
  """
  if tile_type == AIE_TILE_T:
    return list(filter(lambda t: t[1] >= AIE_TILE_ROW_OFFSET, tile_list))
  if tile_type == SHIM_TILE_T:
    return list(filter(lambda t: t[1] == 0, tile_list))
  if tile_type == MEM_TILE_T:
    return list(filter(lambda t: t[1] in list(range(1, AIE_TILE_ROW_OFFSET)), tile_list))
  return tile_list


def parse_overlay():
  """
  Creates overlay dictionary from json file
  """
  overlay = {}
  try:
    with open("src/mldebug/arch/overlays/stx_overlay.json", "r", encoding="utf-8") as f:
      overlay = json.load(f)
    for tile in overlay["overlay_description"]["tiles"]:
      t = (tile["column"], tile["row"])
      overlay[t] = tile["dma_connectivity"]
  except FileNotFoundError:
    # Return empty overlay if not supported
    #print("Overlay info not found for this Device.")
    return {}

  return overlay


FIRMWARE_STATES_MAP = {}
