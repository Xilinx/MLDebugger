# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2026 Advanced Micro Devices, Inc. All rights reserved.

"""
AIE2PS Specific Defs
"""

import json

from .device_configs import DEVICE_CONFIGS

AIE_TILE_T = "aie_tile"
SHIM_TILE_T = "shim_tile"
MEM_TILE_T = "mem_tile"
TILE_TYPES = [AIE_TILE_T, SHIM_TILE_T, MEM_TILE_T]

ARCH_NAME = "aie2ps"

AIE_TILE_ROW_OFFSET = 3
MEM_TILE_SZ = 0x80000
HAS_UC_MODULE = True
HAS_PER_CHANNEL_BD_REGS = {AIE_TILE_T: False, SHIM_TILE_T: False, MEM_TILE_T: False}

"""
For now we assume that we're reading same tile and hence we mask higher logical bits
See aie2p def for full explanation.
"""
LOGICAL_DM_MASK = 0xFFFF

OVERLAY = ""

REGS_DMA_BD = {
  MEM_TILE_T: {"num_bd": 48, "base_addr": 0xA0000, "unused": 0, "bd_regcount": 8},
  SHIM_TILE_T: {"num_bd": 16, "base_addr": 0x9000, "unused": 0, "bd_regcount": 8},
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
  "SPARE_REG": 0xFFFF4,
  "PERF_CNTR_0_CTRL": 0x91000,
  "PERF_CNTR_1_CTRL": 0x91004,
  "PERF_CNTR_2_CTRL": 0x91008,
  "PERF_CNTR_3_CTRL": 0x9100C,
  "PERF_CNTR_4_CTRL": 0x91010,
  "PERF_CNTR_0": 0x91020,
  "PERF_CNTR_1": 0x91024,
  "PERF_CNTR_2": 0x91028,
  "PERF_CNTR_3": 0x9102C,
  "PERF_CNTR_4": 0x91030,
  "PERF_CNTR_5": 0x91034,
}

_create_bds(MEM_TILE_T, Memory_tile_registers)
for _regid in range(0, 64):
  Memory_tile_registers[f"LOCK_VALUE_{_regid}"] = 0x000C0000 + 0x10 * _regid

Core_registers = {
  "CORE_STATUS": 0x38004,
  "DEBUG_CONTROL0": 0x38010,
  "DEBUG_CONTROL1": 0x38014,
  "EVENT_STATUS0": 0x34200,
  "EVENT_STATUS1": 0x34204,
  "EVENT_STATUS2": 0x34208,
  "EVENT_STATUS3": 0x3420C,
  "EDGE_EVENT_CTRL": 0x34408,
  "PERF_CNTR_0_CTRL": 0x37500,
  "PERF_CNTR_1_CTRL": 0x37504,
  "PERF_CNTR_2_CTRL": 0x37508,
  "PERF_CNTR_0": 0x37520,
  "PERF_CNTR_1": 0x37524,
  "PERF_CNTR_2": 0x37528,
  "PERF_CNTR_3": 0x3752C,
  "PERF_CNTR_1_EVENT_VAL": 0x37584,
  "LOCK_OFL": 0x1F120,
  "LOCK_UFL": 0x1F128,
  "CORE_PC": 0x32D00,
  "PC_EVENT0": 0x38020,
  "COMBO_EVENT_INPUTS_A_D": 0x34400,
  "CORE_SR1": 0x32DC0,
  "CORE_SR2": 0x32DD0,
  "ECC_SCRUB_EVENT": 0x38110,
}


def init(device=None):
  """
  Apply device/variant geometry from the central registry.

  `device` is the resolved sub-device name (e.g. 'telluride' or 't20');
  it selects AIE_TILE_ROW_OFFSET (= core_row_start) and MEM_TILE_SZ so
  same-hwGen variants can differ in geometry.
  """
  global AIE_TILE_ROW_OFFSET, MEM_TILE_SZ
  cfg = DEVICE_CONFIGS.get(device)
  if cfg:
    AIE_TILE_ROW_OFFSET = cfg["core_row_start"]
    MEM_TILE_SZ = cfg["mem_tile_sz"]


_create_bds(AIE_TILE_T, Core_registers)
for _regid in range(0, 16):
  Core_registers[f"LOCK_VALUE_{_regid}"] = 0x1F000 + 0x10 * _regid

Shim_tile_registers = {
  "PERF_CNTR_0_CTRL": 0x31000,
  "PERF_CNTR_1_CTRL": 0x31008,
  "PERF_CNTR_2_CTRL": 0x3100C,
  "PERF_CNTR_3_CTRL": 0x31010,
  "PERF_CNTR_4_CTRL": 0x31014,
  "PERF_CNTR_5_CTRL": 0x31018,
  "PERF_CNTR_0": 0x31020,
  "PERF_CNTR_1": 0x31024,
  "PERF_CNTR_2": 0x31028,
  "PERF_CNTR_3": 0x3102C,
  "PERF_CNTR_4": 0x31030,
  "PERF_CNTR_5": 0x31034,
}

_create_bds(SHIM_TILE_T, Shim_tile_registers)
for _regid in range(0, 16):
  Shim_tile_registers[f"LOCK_VALUE_{_regid}"] = 0x00000000 + 0x10 * _regid

Shim_uc_registers = {
  "FW_STATE": 0x880A0,
  "PAGE_INDEX": 0x880A4,
  "OFFSET": 0x880A8,
  "HSA_QUEUE_HIGH_ADDR": 0x8800C,
  "HSA_QUEUE_LOW_ADDR": 0x88010,
  "MDM_DBG_CTRL_STATUS": 0xB0010,
  "CORE_STATUS": 0xC0000,
  "CORE_INTERRUPT_STATUS": 0xC0008,
  "DMA_DM2MM_STATUS": 0xC0100,
  "DMA_MM2DM_STATUS": 0xC0110,
  "AXIMM_Offset": 0xC0020,
  "AXI_MM_OUTSTANDING_TXN": 0xC0024,
}

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
    SHIM_TILE_T: {"SHIM_S2MM0_STATUS": 0x09320, "SHIM_S2MM1_STATUS": 0x09324},
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
      "SHIM_MM2S0_STATUS": 0x09328,
      "SHIM_MM2S1_STATUS": 0x0932C,
    },
    AIE_TILE_T: {"DMA_MM2S0_STATUS": 0x1DF10, "DMA_MM2S1_STATUS": 0x1DF14},
  },
}

# Shim PL-module registers for PLIO (south stream ports). NPU3 has no PL.
# Masters are AIE->PL, slaves are PL->AIE. Addresses from the aie2ps shim PL module.
PLIO_SOUTH_MASTERS = 6
PLIO_SOUTH_SLAVES = 8
REGS_PLIO_STATUS = {
  "STREAM_SWITCH_PARITY_STATUS": 0x3FF10,
  "CONTROL_PACKET_HANDLER_STATUS": 0x3FF30,
  "STREAM_SWITCH_EVENT_PORT_SELECTION_0": 0x3FF00,
  "STREAM_SWITCH_EVENT_PORT_SELECTION_1": 0x3FF04,
  "PL_EVENT_STATUS4": 0x34210,
  "PL_EVENT_STATUS5": 0x34214,
  "PL_INTERFACE_UPSIZER_CONFIG": 0x30000,
  "PL_INTERFACE_DOWNSIZER_CONFIG": 0x30004,
  "PL_INTERFACE_DOWNSIZER_ENABLE": 0x30008,
}
for _i in range(PLIO_SOUTH_MASTERS):
  REGS_PLIO_STATUS[f"SS_MASTER_SOUTH{_i}"] = 0x3F008 + 4 * _i
for _i in range(PLIO_SOUTH_SLAVES):
  REGS_PLIO_STATUS[f"SS_SLAVE_SOUTH{_i}"] = 0x3F108 + 4 * _i

# Physical stream-switch port ids on the shim PL module.
_SHIM_MASTER_PORTS = (
  "CTRL0", "FIFO0",
  "SOUTH0", "SOUTH1", "SOUTH2", "SOUTH3", "SOUTH4", "SOUTH5",
  "WEST0", "WEST1", "WEST2", "WEST3",
  "NORTH0", "NORTH1", "NORTH2", "NORTH3", "NORTH4", "NORTH5",
  "EAST0", "EAST1", "EAST2", "EAST3",
)
_SHIM_SLAVE_PORTS = (
  "CTRL0", "FIFO0",
  "SOUTH0", "SOUTH1", "SOUTH2", "SOUTH3", "SOUTH4", "SOUTH5", "SOUTH6", "SOUTH7",
  "WEST0", "WEST1", "WEST2", "WEST3",
  "NORTH0", "NORTH1", "NORTH2", "NORTH3",
  "EAST0", "EAST1", "EAST2", "EAST3",
  "TRACE0",
)

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
  "Error_Halt",
  "Core_Done",
  "Core_Processor_Bus_Stall",
]

# Events 48-72
error_event_ids = list(range(48, 73))

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
  "DEBUG_HALTED_CORE",
  "ACTIVE_CORE",
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
  "SRS_OVERFLOW_CORE",
  "UPS_OVERFLOW_CORE",
  "FP_HUGE/OVERFLOW_CORE",
  "INT_FP_ZERO/UNDERFLOW_CORE",
  "FP_INVALID_CORE",
  "FP_DIV_BY_ZERO_CORE",
  "RESERVED_CORE",
  "PM_REG_ACCESS_FAILURE_CORE",
  "STREAM_PKT_PARITY_ERROR_CORE",
  "CONTROL_PKT_ERROR_CORE",
  "AXI_MM_SLAVE_ERROR_CORE",
  "INSTR_DECOMPRSN_ERROR_CORE",
  "DM_ADDRESS_OUT_OF_RANGE_CORE",
  "PM_ECC_ERROR_SCRUB_CORRECTED_CORE",
  "PM_ECC_ERROR_SCRUB_2BIT_CORE",
  "PM_ECC_ERROR_1BIT_CORE",
  "PM_ECC_ERROR_2BIT_CORE",
  "PM_ADDRESS_OUT_OF_RANGE_CORE",
  "DM_ACCESS_TO_UNAVAILABLE_CORE",
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
  Parse core event registers
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
  if data >> 27 & 1:
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


def _shim_port_name(ports, idx):
  if 0 <= idx < len(ports):
    return ports[idx]
  return f"ID{idx}"


def _parse_pl_widths(data, num_ports):
  """Decode PL interface width. 0=32-bit, 1=64-bit; the combine bit makes a 128-bit pair."""
  parts = []
  for pair in range(num_ports // 2):
    base = pair * 3
    lo = pair * 2
    if data >> (base + 2) & 1:
      parts.append(f"S{lo}+S{lo + 1}=128")
    else:
      parts.append(f"S{lo}={'64' if data >> base & 1 else '32'}")
      parts.append(f"S{lo + 1}={'64' if data >> (base + 1) & 1 else '32'}")
  return ",".join(parts)


def parse_plio_upsizer(data):
  """AIE to PL width for south masters 0-5."""
  return _parse_pl_widths(data, PLIO_SOUTH_MASTERS)


def parse_plio_downsizer(data):
  """PL to AIE width for south slaves 0-7."""
  return _parse_pl_widths(data, PLIO_SOUTH_SLAVES)


def parse_plio_downsizer_enable(data):
  enabled = [f"S{i}" for i in range(PLIO_SOUTH_SLAVES) if data >> i & 1]
  return ",".join(enabled) if enabled else "none"


def parse_plio_parity(data):
  names = (
    "South0", "South1", "South2", "South3", "South4", "South5", "Tile_Control", "uController"
  )
  bits = [names[i] for i in range(len(names)) if data >> i & 1]
  return ",".join(bits) if bits else "OK"


def parse_plio_ctrl_pkt(data):
  names = (
    "First_Header_Parity_Error", "Second_Header_Parity_Error", "SLVERR_On_Access", "Tlast_Error"
  )
  bits = [names[i] for i in range(len(names)) if data >> i & 1]
  return ",".join(bits) if bits else "OK"


def parse_plio_master(data):
  """South master (AIE to PL). Circuit mode [4:0] is the slave port it reads."""
  if (data >> 31 & 1) == 0:
    return "Disabled"
  mode = "Packet" if data >> 30 & 1 else "Circuit"
  cfg = data & 0x7F
  if data >> 30 & 1:
    extra = f",arb={cfg & 7},msel={(cfg >> 3) & 0xF}"
  else:
    extra = f",src={_shim_port_name(_SHIM_SLAVE_PORTS, cfg & 0x1F)}"
  if data >> 7 & 1:
    extra += ",DropHeader"
  return "Enabled," + mode + extra


def parse_plio_slave(data):
  """South slave (PL to AIE)."""
  if (data >> 31 & 1) == 0:
    return "Disabled"
  mode = "Packet" if data >> 30 & 1 else "Circuit"
  return "Enabled," + mode


def _parse_plio_event_sel(data, port_base):
  parts = []
  for i in range(4):
    field = (data >> (8 * i)) & 0x3F
    pid = field & 0x1F
    is_master = field >> 5 & 1
    ports = _SHIM_MASTER_PORTS if is_master else _SHIM_SLAVE_PORTS
    kind = "master" if is_master else "slave"
    parts.append(f"P{port_base + i}:{kind} {_shim_port_name(ports, pid)}")
  return ",".join(parts)


def parse_plio_event_sel_0(data):
  return _parse_plio_event_sel(data, 0)


def parse_plio_event_sel_1(data):
  return _parse_plio_event_sel(data, 4)


def _parse_plio_port_events(data, event_base):
  """Sticky PL stream-switch port events. Port 0 idle is event 133."""
  names = ("Idle", "Running", "Stalled", "Tlast")
  parts = []
  other = 0
  for bit in range(32):
    if (data >> bit & 1) == 0:
      continue
    ev = event_base + bit
    if 133 <= ev <= 164:
      idx = ev - 133
      parts.append(f"Port{idx // 4}_{names[idx % 4]}")
    else:
      other |= 1 << bit
  if other:
    parts.append(f"other=0x{other:x}")
  return ",".join(parts) if parts else "OK"


def parse_plio_event_status4(data):
  return _parse_plio_port_events(data, 128)


def parse_plio_event_status5(data):
  return _parse_plio_port_events(data, 160)


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
REG_PARSERS["STREAM_SWITCH_PARITY_STATUS"] = parse_plio_parity
REG_PARSERS["CONTROL_PACKET_HANDLER_STATUS"] = parse_plio_ctrl_pkt
REG_PARSERS["STREAM_SWITCH_EVENT_PORT_SELECTION_0"] = parse_plio_event_sel_0
REG_PARSERS["STREAM_SWITCH_EVENT_PORT_SELECTION_1"] = parse_plio_event_sel_1
REG_PARSERS["PL_EVENT_STATUS4"] = parse_plio_event_status4
REG_PARSERS["PL_EVENT_STATUS5"] = parse_plio_event_status5
REG_PARSERS["PL_INTERFACE_UPSIZER_CONFIG"] = parse_plio_upsizer
REG_PARSERS["PL_INTERFACE_DOWNSIZER_CONFIG"] = parse_plio_downsizer
REG_PARSERS["PL_INTERFACE_DOWNSIZER_ENABLE"] = parse_plio_downsizer_enable
for _i in range(PLIO_SOUTH_MASTERS):
  REG_PARSERS[f"SS_MASTER_SOUTH{_i}"] = parse_plio_master
for _i in range(PLIO_SOUTH_SLAVES):
  REG_PARSERS[f"SS_SLAVE_SOUTH{_i}"] = parse_plio_slave


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
    with open("src/mldebug/arch/overlays/telluride_overlay.json", "r", encoding="utf-8") as f:
      overlay = json.load(f)
    for tile in overlay["overlay_description"]["tiles"]:
      t = (tile["column"], tile["row"])
      overlay[t] = tile["dma_connectivity"]
  except FileNotFoundError:
    # Return empty overlay if not supported
    # print("Overlay info not found for this Device.")
    return {}

  return overlay


FIRMWARE_STATES_MAP = {
  0x0FFE: "HANDSHAKE",
  0x0001: "INIT_BARRIER",
  0x0002: "CONFIGURE_PARTITION",
  0x0003: "HSA_CONFIG",
  0x0004: "EXEC_PAGEIN",
  0x0005: "EXEC_INITIAL_PASS",
  0x0006: "EXEC_EVENTLOOP",
  0x0007: "OOO_OPCODE",
  0x0008: "OOO_PREFETCH",
  0x0009: "EMPTY_PAGE",
  0x000A: "WAKENUP_AFTER_PREEMPT",
  0x000B: "EXEC_PAGE",
  0x000C: "CHECK_NEWPAGE",
  0x000D: "RUN_TASK_COMPLETION",
  0x000E: "PREFETCH_IN_ORDER",
  0x000F: "RELOAD_OOO",
  0x0010: "RUN_SAVE",
  0x0011: "RELOAD_CORE",
  0x0012: "NO_PAGE",
  0x0013: "PREFETCH_OOO",
  0x0014: "WAIT_PAGE_LOAD",
  0x0015: "INVALID_PKT",
  0x0FFF: "EXIT",
  0x1001: "LEADER_HOST_QUEUE_POP",
  0x1002: "LEADER_DISTRIBUTE_WORK",
  0x1003: "LEADER_POST_DIST_BARRIER",
  0x1004: "LEADER_POST_WORK_BARRIER",
  0x1005: "LEADER_RUN",
  0x1006: "LEADER_HOST_QUEUE_FINISH",
  0x2001: "WORKER_PRE_WORK_BARRIER",
  0x2002: "WORKER_POST_WORK_BARRIER",
  0x2003: "WORKER_RUN",
  0xDEADBEEF: "FW_STATE_TEST",
}
