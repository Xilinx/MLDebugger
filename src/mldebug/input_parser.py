# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Input config and misc parsing
"""

from dataclasses import dataclass
from pathlib import Path

import glob
import json
import importlib
import os
import subprocess
import sys
import re

from mldebug.arch import load_aie_arch, AIE_DEV_PHX, AIE_DEV_STX, AIE_DEV_TEL
from mldebug.utils import LOGGER, is_aarch64, is_windows

@dataclass
class RunFlags:
  """
  Runtime Flag container
  """

  l1_ofm_dump: bool
  skip_dump: bool
  layer_status: bool
  l2_dump_only: bool
  l2_ifm_dump: bool
  text_dump: bool
  skip_iter: bool
  # Test Flags
  mock_hang: bool
  dump_temps: bool
  multistamp: bool
  enable_tg: bool


@dataclass
class Subgraph:
  """VAIML Subgraph"""

  folder_path: str
  name: str


def create_run_flags(args, subgraph_path: str, fsp: str, fsp_execution_order: list[str]) -> None:
  """
  Updates the work directory, buffer info, and sets device and run flags
  when running a partition. Parses configuration files if needed.

  Args:
      args: Argument object to be updated with relevant fields.
      subgraph_path (str): The path to the subgraph.
      fsp (str): The current failsafe partition name/id.
      fsp_execution_order (list[str]): Execution order of all failsafe partitions.

  Returns:
      None
  """
  args.external_buffer_id = None
  args.flexmlrt_hsi = None
  args.debug_map_json = None
  args.mladf_report = None

  # AIE Work dir, device, buffer info
  if subgraph_path and os.path.exists(args.vaiml_folder_path):
    args.aie_dir = subgraph_path + f"/{fsp}/aiecompiler/Work"
    args.mladf_report = subgraph_path + f"/{fsp}/aiecompiler/Work/reports/mladf_compiler_report.json"
    args.buffer_info = subgraph_path + f"/{fsp}/buffer_info.json"
    args.flexmlrt_hsi = subgraph_path + f"/{fsp}/flexmlrt-hsi.json"
    args.debug_map_json = subgraph_path + f"/{fsp}/debug_map.json"
    args.fsp_execution_order = fsp_execution_order
    args.fsp = fsp
    args.last_fsp = args.fsp == args.fsp_execution_order[-1]

  if args.x2_folder_path and os.path.exists(args.x2_folder_path):
    args.aie_dir = args.x2_folder_path
    args.buffer_info = args.x2_folder_path + "/buffer_info.json"
    args.external_buffer_id = args.x2_folder_path + "/external_buffer_id.json"
    args.subgraph_name = None

  set_device(args)

  # Metadata check
  no_metadata = args.buffer_info is None or not os.path.exists(args.buffer_info)
  if (no_metadata or not os.path.exists(args.aie_dir)) and not args.aie_only:
    print("[INFO] Using Standalone mode.")
    args.aie_only=True
    args.interactive=True

  # AIE interface for aie2p and aie2 are shared
  # We need to differentiate between them for a few items
  args.aie_iface = load_aie_arch(args.device)
  args.aie_iface.init(args.device == AIE_DEV_PHX)

  # Finally Create run flags
  if isinstance(args.run_flags, RunFlags):
    return

  def get_flag(s, default=False):
    """
    Helper to extract the value of a run flag from args.run_flags.
    Returns ``default`` when the flag is not specified in args.run_flags.
    """
    if not args.run_flags or s not in args.run_flags:
      return default
    return True

  args.run_flags = RunFlags(
    get_flag("l1_ofm_dump"),
    get_flag("skip_dump"),
    get_flag("layer_status"),
    get_flag("l2_dump_only"),
    get_flag("l2_ifm_dump"),
    get_flag("text_dump"),
    get_flag("skip_iter"),
    get_flag("mock_hang"),
    get_flag("dump_temps"),
    get_flag("multistamp"),
    get_flag("enable_tg", default=True)
  )


def check_registry_keys(npu3=False) -> None:
  """
  Checks if specific registry keys are correctly configured on Windows,
  and sets values if necessary for MLDebug operation. Exits on failure
  or after making modifications.

  Args:
      npu3 (bool): Whether to check npu3-specific registry keys.

  Returns:
      None
  """
  if not is_windows():
    return

  winreg = importlib.import_module("winreg")
  hive = winreg.HKEY_LOCAL_MACHINE
  if not npu3:
    items_to_check = [
      ("SYSTEM\\ControlSet001\\Services\\IpuMcdmDriver", "PowerManagementEnable", 0),
      ("SYSTEM\\ControlSet001\\Services\\IpuMcdmDriver", "PartitionCtxIdleTimeout", 10000),
      ("SYSTEM\\ControlSet001\\Services\\IpuMcdmDriver", "AieMemoryReadWriteEnable", 1),
      ("SYSTEM\\ControlSet001\\Control\\GraphicsDrivers", "TdrDelay", 10000),
    ]
  else:
    items_to_check = [
      ("SYSTEM\\ControlSet001\\Services\\Npu2McdmDriver", "AieMemoryReadWriteEnable", 1),
      ("SYSTEM\\ControlSet001\\Control\\GraphicsDrivers", "TdrDelay", 10000),
    ]
  modified = False

  for key_path, value_name, expected_value in items_to_check:
    try:
      with winreg.OpenKey(hive, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
        regstring = f"HKEY_LOCAL_MACHINE\\{key_path}\\{value_name}"
        try:
          current_value, _ = winreg.QueryValueEx(key, value_name)
          if current_value != expected_value:
            raise WindowsError
        except WindowsError:
          winreg.SetValueEx(key, value_name, 0, winreg.REG_DWORD, expected_value)
          LOGGER.log(f"Created registry key: {regstring} with value {expected_value}")
          modified = True
    except WindowsError:
      LOGGER.log(
        f"Error: Unable to access or create registry key:"
        f" HKEY_LOCAL_MACHINE\\{key_path}. Please run tool with admin privileges."
      )
      sys.exit(1)
    except ValueError:
      LOGGER.log(f"Error: Invalid registry key format: {key_path}")
      sys.exit(1)

  if modified:
    LOGGER.log(
      "\nRegistry settings to enable MlDebug were modified. Please restart your machine for the changes to take effect."
    )
    sys.exit(1)
  else:
    LOGGER.log("\nRegistry settings check passed. No modifications were necessary.")


def set_device(args) -> None:
  """
  Detects and sets the device target (phx, stx, or tel) for the current work directory.

  Args:
      args: Argument object that is updated to set the detected device.

  Returns:
      None
  """
  endmsg = "\n"
  if not args.device:
    endmsg = " Use -d to specify a diferent device.\n"
    # if on ARM, default is telluride else STX
    args.device = AIE_DEV_TEL if is_aarch64() else AIE_DEV_STX
    genstr = "XAIE_DEV_GEN_AIE2P"

    ctrl_cpp = args.aie_dir + "/ps/c_rts/aie_control.cpp"
    try:
      with open(ctrl_cpp, encoding="utf-8") as f:
        data = f.read().split("\n")
        for line in data:
          if "#define HW_GEN" in line:
            genstr = line.split(" ")[-1]
            break
        if genstr == "XAIE_DEV_GEN_AIE2PS":
          args.device = AIE_DEV_TEL
        if genstr == "XAIE_DEV_GEN_AIE2":
          args.device = AIE_DEV_PHX
    except (FileNotFoundError, KeyError):
      pass
      #LOGGER.log("[INFO] Unable to detect device automatically.")
  print(f"[INFO] Using AIE Device: {args.device}.", end=endmsg)


def print_hw_context_table(current_contexts: dict[str, dict[str, str]]) -> None:
  """
  Prints the current hardware contexts in a table format.

  Args:
      current_contexts (dict): Dictionary with context IDs as keys and
                               dicts as values including columns, pid, and status.

  Returns:
      None
  """
  # LOGGER.log header
  LOGGER.log(f"{'Context ID':<12} {'Columns':<30} {'PID':<12} {'Status':<12}")

  # LOGGER.log table data
  for context, context_data in current_contexts.items():
    columns_str = ", ".join(map(str, context_data["columns"]))
    LOGGER.log(f"{context:<12} {columns_str:<30} {context_data['pid']:<12} {context_data['status']:<12}")


def check_hw_context(device: str) -> tuple[int, int]:
  """
  Finds and returns the hardware context and process ID from the xrt-smi command output.

  If xrt-smi fails or no application is running, prompts the user to input ctx and pid manually.

  Args:
      device (str): Device identifier.

  Returns:
      Tuple[int, int]: Selected context ID and PID.
  """
  filename = "xrt-smi_output.json"
  use_shell = is_windows()

  # Build xrt-smi command, adding device argument for telluride
  device_arg = "-d 0000:00:00.0" if device == AIE_DEV_TEL else "-d"
  cmd = f"xrt-smi examine -r aie-partitions {device_arg} -f JSON -o {filename} --force".split()
  ctx, pid = (0, 0)
  try:
    subprocess.run(cmd, stdout=subprocess.PIPE, shell=use_shell, check=True).stdout.decode("utf-8")
    with open(filename, "r", encoding="utf-8") as f:
      data = json.load(f)
    partitions = data["devices"][0]["aie_partitions"]["partitions"]
    current_contexts = {}

    for partition in partitions:
      start_col, num_cols = int(partition["start_col"]), int(partition["num_cols"])
      columns = [i + start_col for i in range(num_cols)]
      for context in partition["hw_contexts"]:
        current_contexts[context["context_id"]] = {
          "columns": columns,
          "pid": context["pid"],
          "status": context["status"],
        }

    if not current_contexts:
      print("Warning: xrt-smi could find no applications running. Please launch an application to use MLDebugger.")
      raise FileNotFoundError
    if len(current_contexts) == 1:
      ctx = int(list(current_contexts.keys())[0])
      pid = int(list(current_contexts.values())[0]["pid"])
    else:
      print_hw_context_table(current_contexts)
      # Ask user
      selected_context_id = input("Multiple Contexts Found. Please enter the Context ID you want to select: ")
      if selected_context_id in current_contexts:
        ctx = int(selected_context_id)
        pid = int(current_contexts[selected_context_id]["pid"])
      else:
        LOGGER.log("Could not find the provided context, Exiting now.")
        sys.exit(1)
  except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
    LOGGER.log("Error with xrt-smi. Please enter ctx, pid manually.")
    pid = int(input("Enter PID > "))
    ctx = int(input("Enter CTX ID > "))
  return ctx, pid


def return_all_subgraphs(vaiml_folder_path: str) -> tuple[str, list[Subgraph]]:
  """
  Finds all subgraphs within a VAIML folder.

  Args:
      vaiml_folder_path (str): Path to the top-level VAIML folder.

  Returns:
      Tuple containing:
        - model_folder_name (str): Name of the model folder containing the subgraphs.
        - subgraph_folder_names (list[Subgraph]): List of Subgraph dataclass instances.
  """
  subgraph_dirs = glob.glob(f"{vaiml_folder_path}/*/vaiml_par_*")
  # glob adds backslashes on win
  subgraph_dirs = [s.replace("\\", "/") for s in subgraph_dirs]
  model_folder_name = ""
  subgraph_folder_names = []
  # LOGGER.log(par_dirs)
  if subgraph_dirs:
    model_folder_name = subgraph_dirs[0].split("/")[-2]
  for s in subgraph_dirs:
    subgraph_folder_names.append(Subgraph(name=s.split("/")[-1], folder_path=s))
  return model_folder_name, subgraph_folder_names


def get_failsafe_partitions(subgraph_path: str) -> list[str]:
  """
  Returns the execution order of the failsafe partitions in the subgraph.

  Args:
      subgraph_path (str): Path to the subgraph directory.

  Returns:
      List of partition indices as strings, extracted from the call order.
  Raises:
      ValueError: If partition name format is invalid.
  """
  with open(f"{subgraph_path}/partition-info.json", "r", encoding="utf-8") as f:
    data = json.load(f)

  result = []
  if "aie_partition_call_order" in data:
    for fsp in data["aie_partition_call_order"]:
      match = re.search(r"_part_(\d+)$", fsp)
      if match:
        result.append(match.group(1))
      else:
        raise ValueError(f"Invalid partition name format: {fsp}")
  else:
    # No need to add warning as fsp is deprecated
    result.append("0")
  return result


def get_subgraph(args) -> tuple[str, Subgraph]:
  """
  Determines if the design is a single-subgraph design or finds the subgraph specified in vitisai_config.json.

  Args:
      args: Argument object containing at least vaiml_folder_path.

  Returns:
      Tuple[str, Subgraph]: Model folder name and the relevant Subgraph instance.

  Raises:
      RuntimeError: If multiple subgraphs are found and none is specified, or if none are found.
  """
  vaiml_folder_path = args.vaiml_folder_path
  model_folder_name, subgraphs = return_all_subgraphs(vaiml_folder_path)
  cfg_file = f"{vaiml_folder_path}/vitisai_config.json"

  if Path(cfg_file).exists():
    with open(cfg_file, "r", encoding="utf-8") as f:
      data = json.load(f)
    vaiml_config = None
    for pas in data["passes"]:
      if "vaiml_config" in pas:
        vaiml_config = pas["vaiml_config"]
        vaiml_subgraphs = vaiml_config.get("include_subgraphs")
        if vaiml_subgraphs:
          # If the include_subgraphs option is present, make sure only one subgraph is present
          if len(vaiml_subgraphs) != 1:
            raise RuntimeError(
              "Error: Multiple subgraphs found. "
              "Please specify the partition by using include_subgraphs "
              "flag in vaiml_config in vitisai_config.json"
            )
          return model_folder_name, Subgraph(
            folder_path=f"{vaiml_folder_path}/{model_folder_name}/{vaiml_subgraphs[0]}", name=vaiml_subgraphs[0]
          )
        break

  if len(subgraphs) > 1:
    raise RuntimeError("Error: Multi-partition design detected. Specify a partition in vitisai_config.json")
  if len(subgraphs) == 0:
    raise RuntimeError("Error: No partition found in the input model folder")
  return model_folder_name, subgraphs[0]
