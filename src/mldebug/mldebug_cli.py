#! /usr/bin/env python3
"""
SPDX-License-Identifier: Apache-2.0
Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

Author(s): Anurag Dubey, Nishant Mysore

User callable top script
"""

import argparse
from argparse import RawTextHelpFormatter
from ast import literal_eval
import os
import time

from mldebug.arch import AIE_DEV_PHX, AIE_DEV_STX, AIE_DEV_TEL, AIE_DEV_NPU3
from mldebug.client_debug import ClientDebug
from mldebug.input_parser import (
  check_hw_context,
  check_registry_keys,
  get_subgraph,
  get_failsafe_partitions,
  create_run_flags,
)
from mldebug.interactive_prompt import InteractivePrompt
from mldebug.utils import setup_logger, close_logger, is_windows


def _apply_unsupported_kernels_from_args(args):
  """
  Append user-provided unsupported kernel names to layer_info.unsupported_superkernels.
  Supports both:
    --unsupported_kernels k1 k2
  and:
    --unsupported_kernels k1,k2

  This must happen before LayerInfo creates Layer objects (ClientDebug -> LayerInfo).
  """
  from mldebug import layer_info # pylint: disable=import-outside-toplevel
  values = args.unsupported_kernels
  if not values:
    return
  for v in values:
    if v is None:
      continue
    # Allow comma-separated lists in a single argv token.
    for token in str(v).split(","):
      token = token.strip()
      if token:
        layer_info.unsupported_superkernels.append(token.lower())


def check_args(args):
  """
  Check argument rules

  Validates and processes the command-line arguments for the debug session.
  Enforces console mode for AIE-only, and parses the tiles argument if present.

  Args:
      args: argparse.Namespace object holding user-provided arguments.

  Returns:
      bool: True if argument check passes, False otherwise.

  Side Effects:
      Prints user warnings or messages about argument handling.
  """
  if args.dump_aie_status:
    args.interactive = False
    args.aie_only = True
    print("[INFO] Dumping advanced AIE status and exiting (non-interactive)")
  elif args.exec_cmd is not None:
    args.aie_only = True
    args.interactive = False
  elif args.aie_only:
    args.interactive = True
    print("[INFO] Using interactive console for AIE-only mode")

  if args.tiles:
    args.tiles = literal_eval(args.tiles)
  if args.core_dump:
    args.backend = "core_dump"
    args.aie_only = True
    if not args.dump_aie_status:
      args.interactive = True
    print("[INFO] Using standalone mode for core dumps")
  if args.backend == "core_dump" and args.core_dump is None:
    print("[ERROR] Core dump file is required when backend is 'core_dump'. Please use -h or --help for usage")
  if args.device == AIE_DEV_NPU3 and not args.overlay:
    args.overlay = "3x4"
  return True


def debug(args, timestamp, subgraph_name=None, fsp="0", folder_name=None):
  """
  Launches the debugger with or without a partition.

  Starts a debug session either for a single run or for a specific subgraph and partition.
  Optionally sets the output directory, propagates context into the argument objects, and
  invokes the debug launch workflow.

  Args:
      args: Argument namespace, parsed from command line.
      timestamp (str): Timestamp string for output organization.
      subgraph_name (str, optional): Name of subgraph to debug (default: None)
      fsp (str, optional): Failsafe partition (default: "0")
      folder_name (str, optional): Model folder name for organization (default: None)

  Side Effects:
      Prints status messages and triggers the actual debug launch routine.
  """
  if subgraph_name and folder_name:
    print(f"Debugging New Subgraph: {subgraph_name}\n")
    print(f"Debugging New Failsafe Partition: {fsp}\n")
    output_dir = f"{folder_name}_{timestamp}/{subgraph_name}/{fsp}"
    args.subgraph_name = subgraph_name
  else:
    output_dir = f"output_{time.strftime('%m%d%H%M%S')}"

  if args.output_dir is not None:
    output_dir = args.output_dir + "/" + output_dir
  launch_debug(args, output_dir)


def launch_debug(args, output_dir):
  """
  Launch Debug after all the setup is done.

  Instantiates the debug handle (ClientDebug) based on the user's configuration,
  resolves the context ID and PID if needed, and hands off to either the
  interactive prompt or batch dump mode.

  Args:
      args: Argument namespace containing user and workflow options.
      output_dir (str): Output directory for dump files and logs.

  Side Effects:
      Creates a debug handle, possibly interacts with hardware,
      and either launches an interactive or batch session.
  """
  # Get context ID for the app
  context_id = 0
  pid = 0
  if args.backend == "xrt":
    context_id, pid = check_hw_context(args.device)
  # Top debug handle
  _apply_unsupported_kernels_from_args(args)
  handle = ClientDebug(args, context_id, pid, output_dir)
  if args.dump_aie_status:
    handle.status_handle.get(
      args.dump_aie_status,
      advanced=True,
      guidance=False
    )
    print(f"[INFO] Advanced AIE status written to {args.dump_aie_status}")
    return
  if args.exec_cmd is not None:
    InteractivePrompt(handle).exec_cmd(args.exec_cmd)
    return
  # Launch Debug
  if args.interactive:
    InteractivePrompt(handle).run()
  else:
    handle.execute_and_dump()

def _dev_cli_help(text):
  """
  Show help text only when ENABLE_DEV is set (e.g. via mldebug.py launcher).
  """
  v = os.environ.get("ENABLE_DEV", "").strip().lower()
  if v in ("1", "true", "yes", "on"):
    return text
  return argparse.SUPPRESS

def app():
  """
  Entry Point

  Orchestrates the setup and launching of the VAIML AIE Debug session.
  Parses and validates command-line arguments, sets up the logger, determines
  subgraph and failsafe execution order, and drives the main debug loop
  including registry checking and the debug workflow.

  Side Effects:
      Parses command-line arguments, validates user input, sets up the logger,
      and invokes debug, possibly multiple times for multi-partition execution.
      Closes the logger at the end of execution.
  """
  top_msg = "AIE Debug for VAIML.\nDefault data dump mode is binary. Files have 8byte header specifying total bytes."
  p = argparse.ArgumentParser(description=top_msg, formatter_class=RawTextHelpFormatter)

  p.add_argument(
    "-b",
    "--buffer_info",
    help=_dev_cli_help("Path to buffer_info.json from VAIML build.\n"),
    required=False,
    metavar="<file>",
  )
  p.add_argument("-a", "--aie_dir", help="Path to AIE Work Directory. Default: Work", default="Work", metavar="<file>")
  # Hidden Argument
  # XRT backend is applicable on the Client host.
  # Test backend is for internal testing
  # Core_dump backend is for reading from the core_dump file
  p.add_argument("-x", "--backend", help=argparse.SUPPRESS, choices=["xrt", "test", "core_dump"], default="xrt")
  p.add_argument("-c", "--core_dump",
               help="Run standalone mode for core-dump inspection.\n"
               "Use -d flag to specify device.",
               type=str,
               metavar = "COREDUMP_FILE")
  p.add_argument(
    "--dump-aie-status",
    dest="dump_aie_status",
    metavar="<output_file_name>",
    help="Write AIE status to a file and exit.\n",
    default=None,
  )
  p.add_argument("--no_header", action="store_true",
               help="Assume raw core dump without header. Use with -c.")
  # Hidden Argument
  # 'AIE Device type'
  p.add_argument(
    "-d",
    "--device",
    help="Specify device if it can't be detected from aie_dir.",
    choices=[AIE_DEV_PHX, AIE_DEV_STX, AIE_DEV_TEL, AIE_DEV_NPU3],
    required=False,
  )
  # Hidden Argument
  # help='Select tiles within overlay. Example: "{(0,2),(0,3)}"\n'
  #               'Default: All tiles',
  p.add_argument("-t", "--tiles", help=argparse.SUPPRESS)
  # Hidden Argument
  p.add_argument("--verbose", action="store_true", help=argparse.SUPPRESS)
  p.add_argument("--flush_disabled", action="store_true", help=argparse.SUPPRESS)
  # Hidden Argument
  # Log output to log_<name>.txt
  # aie_status to aie_status_<name>.txt
  p.add_argument("-n", "--name", help=argparse.SUPPRESS, required=False, metavar="<name>")
  p.add_argument("-o", "--overlay", help="Overlay used by design. Default: 4x4", metavar="<cxr>")
  p.add_argument("-i", "--interactive", action="store_true", help="Launch in Interactive Mode. Default: Batch")
  p.add_argument(
    "-l",
    "--output_dir",
    help="Directory to store memory and status dumps.\nDefault : layer_dump",
    default="layer_dump",
    metavar="<dir>",
  )
  p.add_argument(
    "-v",
    "--vaiml_folder_path",
    help="Specify the VAIML top level folder path.\nThis overrides aie_dir and buffer_info.\n",
    required=False,
    metavar="<path>",
  )
  p.add_argument(
    "-x2",
    "--x2_folder_path",
    help=_dev_cli_help(
      "Specify the X2 top level folder path.\nThis overrides aie_dir and buffer_info.\n"
    ),
    required=False,
    metavar="<path>",
  )
  p.add_argument(
    "-s", "--aie_only", action="store_true", help="Standalone AIE debug. Work dir can be optionally specified."
  )
  p.add_argument(
    "--exec_cmd",
    dest="exec_cmd",
    default=None,
    metavar="<command>",
    help="Execute a command in the advanced shell (-s) and exit.",
  )
  p.add_argument(
    "-e", "--exit_at_layer", type=int,
    #help="Run until this layer and exit in batch mode.",
    help=argparse.SUPPRESS,
    metavar="LAYER"
  )
  p.add_argument(
    "-l3",
    "--l3",
    action="store_true",
    help="Dumps L3 buffers during the execution",
  )
  p.add_argument(
    "-auto",
    "--automated_debug",
    action="store_true",
    help=argparse.SUPPRESS,
    # This was needed for fsp
    #help="Coordinate with flexmlrt to automatically run the design. Run with ENABLE_ML_DEBUG=3",
  )
  # Hidden Argument
  # Use this tool with AIESim
  p.add_argument("--aiesim", action="store_true", help=argparse.SUPPRESS)
  p.add_argument(
    "--peano",
    action="store_true",
    help=_dev_cli_help(
      "Enable support for peano.\nWith -v flag, peano support is autodetected."
    ),
  )
  p.add_argument(
    "--unsupported_kernels",
    dest="unsupported_kernels",
    nargs="*",
    default=None,
    metavar="KERNEL",
    help=argparse.SUPPRESS,
    #help="Additional kernel names to treat as unsupported and skip during execution.\n"
    #"Example: --unsupported_kernels conv2d_maxpool superkernel_clip1d\n",
  )
  p.add_argument(
    "-f",
    "--run_flags",
    nargs="*",
    choices=[
      "skip_end_pc",
      "skip_dump",
      "layer_status",
      "l2_dump_only",
      "mock_hang",
      "l2_ifm_dump",
      "text_dump",
      "l1_ofm_dump",
      "skip_iter",
      "dump_temps",
      "multistamp",
      "enable_tg"
    ],
    help="Specify one or more runtime flags:\n"
    "skip_dump       : Do not dump memory\n"
    #"layer_status    : Dump AIE status at start of each layer\n"
    #"l2_dump_only    : Dump only L2 buffers\n"
    "l2_ifm_dump     : Dump only L2 IFM buffers\n"
    "l1_ofm_dump     : Dump L1 ofm buffers in addition to others\n"
    "text_dump       : Dump in text format\n"
    "skip_iter       : Skip iterations in batch mode when possible\n"
    #"dump_temps      : Write intermediate (.lst) files to disk\n"
    "multistamp      : Enable N Stamp/Batch mode\n",
    #"enable_tg       : Enable Step to TG layers\n",
    # 'mock_hang'    : Simulate hang at one of the layers in test mode
    metavar="<flag1> <flag2>",
  )
  args = p.parse_args()
  setup_logger(args)

  if not check_args(args):
    print("Argument check failed")
    return

  subgraph_name = None
  subgraph_folder_path = ""
  model_folder_name = None
  fsp_execution_order = ["0"]

  if args.vaiml_folder_path:
    model_folder_name, subgraph = get_subgraph(args)
    subgraph_name, subgraph_folder_path = subgraph.name, subgraph.folder_path
    fsp_execution_order = get_failsafe_partitions(subgraph_folder_path)

  timestamp = time.strftime("%m%d%H%M%S")
  registry_checked = False
  for fsp in fsp_execution_order:
    create_run_flags(args, subgraph_folder_path, fsp, fsp_execution_order)
    if not registry_checked and args.backend == "xrt" and is_windows():
      check_registry_keys(args.device == AIE_DEV_NPU3)
      registry_checked = True
    debug(args, timestamp, subgraph_name, fsp, model_folder_name)
    if args.dump_aie_status:
      break

  # End Debug
  close_logger()


if __name__ == "__main__":
  app()
