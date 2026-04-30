# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Interactive prompt shells for AIE debugging.

Provides the user-facing command loops for both the basic interactive mode
(text command shortcuts) and the advanced Python shell (direct function access).
"""

import builtins
import code

try:
  import readline  # noqa: F401  -- enables arrow-key history in the shell
except ModuleNotFoundError:
  pass


class InteractivePrompt:
  """
  User-facing interactive shells for AIE debugging.

  Wraps a ClientDebug handle and provides two prompt modes:
    run()          -- basic text command loop (step/next/breakpoint/continue)
    run_advanced() -- full Python shell with debugging primitives exposed
  """

  def __init__(self, handle):
    """
    Args:
      handle: ClientDebug instance providing the full debugging API.
    """
    self.handle = handle

  # ------------------------------------------------------------------ #
  # Advanced Python shell
  # ------------------------------------------------------------------ #

  def _build_shell_namespace(self):
    """
    Build the variable namespace and help text for the advanced shell.

    Returns:
      (namespace_dict, help_text): namespace_dict merges module globals with
      the debugging helper bindings; help_text is the help string shown in
      interactive mode.
    """
    h = self.handle
    aie_only = h.args.aie_only

    help_text = """
Available Functions in Advanced/AIE Only Mode
Type help(command) for more information about a specific command.

INFORMATION FUNCTIONS
  help()              : Print this Message
  info()              : Print AIE Core state and current breakpoints
  funcs(elf_id=None)  : Print AIE Functions for a particular or all elfs
  calltree(sid=0)     : Print CallTree(s) for a particular or all stamps
  dump_lst(sid=0)     : Dump LST file(s) for a particular or all stamps

AIE INSPECTION FUNCTIONS
  status(filename=None, advanced=False,  : Print or dump Status for : AIE, Memory and Interface Tiles
         guidance=False)                   Advanced flag dumps detailed BD and Lock information
                                           Note: Set guidance=True to run guidance checks
  uc_status()                            : Print UC status
                                           Note: Set guidance=True to run guidance checks
  rmem(col, row, offset, size,
       filename=None)                    : Read or dump memory
  rreg(col, row, offset)                 : Read a register
  preg(col, row, offset)                 : Print a register
  wreg(col, row, offset, value)          : Write to a register
  dump_l3(name, offset, size)            : Dump L3 spill buffer to file (Run VAIML with ENABLE_ML_DEBUG=2)
  control_instr()                        : Print instr in aie_runtime_control (--multi-layer-record-txn required)
  rlcp(col=None, row=None, ping=1)       : Read LCP from a core (Default: ping lcp on first core of stamp 0)

STAMP LEVEL CONTROL FUNCTIONS (Default: Leftmost)
  swreg(offset, val, sid=0) : Write all registers
  srreg(offset, sid=0)      : Read all registers
  pc(sid=0)                 : Read current Program Counter from AIE tiles

DIRECT AIE CONTROL FUNCTIONS
  unhalt()             : Unhalt all AIE Cores
  pc_brkpt(pc, slot)   : Set breakpoint on a given pc. Available slots: 0,1
  clear_pc_brkpt(slot) : Clear PC breakpoint. slot=0,1 or omit to clear both
  step(num_instr=1)    : Single Step AIE Cores
  goto_pc(pc)          : Execute AIE cores untill a given pc is hit
"""

    if not aie_only:
      help_text += """
LAYER BASED AIE CONTROL FUNCTIONS (-b or -v or -x2 flags required)
  dump_buffers()            : Dump AIE Buffers at current state
  step_it()                 : Step to next Iteration
  step_layer()              : Step to next Layer
  add_brkpt(layer, itr=1)   : Add Breakpoint at a layer and iteration. Default itr: 1
  cont()                    : Continue execution till next breakpoint/final Layer
"""

    # Expose essential debug functions and symbols
    # pylint: disable=possibly-unused-variable
    info = h.print_core_summary
    funcs = h.design_info.print_aie_functions
    step_layer = h.step_layer
    step_it = h.step_iter_manual
    cont = h.continue_execution
    dump_buffers = h.dump_memory
    dump_l3 = h.dump_l3_buffers_manual
    rmem = h.impl.dump_memory
    rreg = h.impl.read_register
    preg = h.impl.print_register
    wreg = h.impl.write_register
    control_instr = h.aie_utls[0].read_control_instr
    add_brkpt = h.add_breakpoint
    status = h.status_handle.get
    uc_status = h.status_handle.get_uc_status
    goto_pc = h.goto_pc
    unhalt = h.impl.continue_aie
    step = h.impl.single_step
    pc_brkpt = h.set_pc_breakpoint_manual
    clear_pc_brkpt = h.clear_pc_breakpoint_manual
    pc = h.read_core_pc_manual
    rlcp = h.read_lcp
    swreg = h.wreg_stamp
    srreg = h.rreg_stamp
    calltree = h.design_info.work_dir.print_calltree
    dump_lst = h.design_info.work_dir.dump_lst_to_file
    # pylint: enable=possibly-unused-variable

    variables = globals().copy()
    variables.update(locals())
    return variables, help_text

  def run_advanced(self):
    """
    Launch an advanced interactive Python shell with debugging helpers.

    Exposes inspection, register, breakpoint, and execution control functions
    as local variables in a code.InteractiveConsole session.
    """
    variables, help_text = self._build_shell_namespace()

    original_help = builtins.help

    def custom_help(*args, **kwargs):
      if not args:
        print(help_text)
      else:
        original_help(*args, **kwargs)

    builtins.help = custom_help

    help()
    shell = code.InteractiveConsole(variables)
    shell.interact()

  def exec_cmd(self, cmd):
    """
    Execute a single command string in the advanced shell namespace and exit.

    Builds the same namespace used by ``run_advanced`` and evaluates ``cmd``
    against it. If ``cmd`` is a single expression, its value is printed
    (when not ``None``); otherwise it is executed as statements. Intended
    for non-interactive standalone-mode invocations (``--exec_cmd``).

    Args:
      cmd (str): Python source to execute. May be an expression or one or
        more statements.
    """
    variables, _ = self._build_shell_namespace()
    try:
      code_obj = compile(cmd, "<exec_cmd>", "eval")
    except SyntaxError:
      exec(compile(cmd, "<exec_cmd>", "exec"), variables)  # pylint: disable=exec-used
    else:
      result = eval(code_obj, variables)  # pylint: disable=eval-used
      if result is not None:
        print(repr(result))

  # ------------------------------------------------------------------ #
  # Basic interactive command loop
  # ------------------------------------------------------------------ #

  def run(self):
    """
    Launch the basic interactive CLI prompt for VAIML/X2 debugging.

    Presents a text-based command loop with shortcuts for stepping,
    breakpoints, inspection, and memory dumps.  Typing ``py`` switches
    into the advanced Python shell.
    """
    h = self.handle
    args = h.args

    if args.aie_only:
      self.run_advanced()
      return

    if not h.initialize_aie():
      return

    help_text = """
Available Commands in Interactive Mode

INFORMATION COMMANDS
  h/help         :    Print this Message
  i/info <Layer> :    Print current state of Execution
AIE INSPECTION COMMANDS
  a/aie_status  <filename>      :    Print Status for : AIE, Memory and Interface Tiles
                                     Specify optional Filename
                                     Note: Add guidance=True to run guidance checks
  v/vaiml_status <filename>     :    Print VAIML Status for : AIE, Memory and Interface Tiles
                                     Specify optional Filename
                                     Note: Add guidance=True to run guidance checks
  u/uc_status                   :    Print UC status
                                     Note: Add guidance=True to run guidance checks
  p/core_pc                     :    Read and display core PCs for all stamps
  d/dump_buffers                :    Dump AIE L1,L2 buffers at current state
  l3/dump_l3                    :    Dump L3 buffer to file (Run application with ENABLE_ML_DEBUG=2)
  r/read_perf_ctrs <c> <r>      :    Read Performance Counter Values
AIE CONTROL COMMANDS
  s/step                        :    Step Iteration
  n/next                        :    Next Layer
  b/breakpoint <layer> <itr=1>  :    Add Breakpoint at a layer and iteration(Default: 1)
                                     Example: b 2 3
  c/continue                    :    Continue execution
  g/goto_pc <pc>                :    Goto Specified PC
                                     Example: g 1234
MISC COMMANDS
  py                            :    Launch interactive python shell with advanced functionality
  q/quit                        :    Quit the program
"""
    print(help_text)
    while True:
      cmd = input("> ").strip().split(" ")
      nargs = len(cmd)
      c = cmd[0]
      if c in ["h", "help"]:
        print(help_text)
      elif c in ["i", "info"]:
        if nargs == 2:
          h.print_current_state(layer_order=int(cmd[1]))
        else:
          h.print_current_state()
      elif c in ["a", "aie_status"]:
        if nargs == 1:
          h.status_handle.get()
        elif nargs == 2:
          h.status_handle.get(filename=cmd[1])
        else:
          print("Unrecognized Parameters. Use h/help")
      elif c in ["u", "uc_status"]:
        h.status_handle.get_uc_status()
      elif c in ["p", "core_pc"]:
        print(h.read_core_pc_manual())
      elif c in ["v", "vaiml_status"]:
        if nargs == 1:
          h.status_handle.get_vaiml_status()
        elif nargs == 2:
          h.status_handle.get_vaiml_status(filename=cmd[1])
        else:
          print("Unrecognized Parameters. Use h/help")
      elif c in ["d", "dump_buffers"]:
        h.dump_memory()
      elif c in ["l3", "dump_l3"]:
        if nargs == 1:
          h.dump_l3_buffers_interactive()
        else:
          print("Invalid input format.")
      elif c in ["r", "read_perf_ctrs"]:
        if nargs == 3 and cmd[1].isnumeric() and cmd[2].isnumeric():
          h.aie_utls[0].read_performance_counters(int(cmd[1]), int(cmd[2]))
        else:
          print("Invalid input format")
      elif c in ["s", "step"]:
        h.step_iter_manual()
      elif c in ["n", "next"]:
        h.step_layer()
      elif c in ["b", "breakpoint"]:
        if nargs == 2 and cmd[1].isnumeric():
          h.add_breakpoint(int(cmd[1]))
        elif nargs == 3 and cmd[1].isnumeric() and cmd[2].isnumeric():
          h.add_breakpoint(int(cmd[1]), int(cmd[2]))
        else:
          print("Unrecognized Parameters. Use h/help")
      elif c in ["c", "continue"]:
        h.continue_execution()
      elif c in ["g", "goto_pc"]:
        if nargs == 2 and cmd[1].isnumeric():
          h.goto_pc(int(cmd[1]))
        else:
          print("Unrecognized Parameters. Use h/help")
      elif c in ["py"]:
        self.run_advanced()
      elif c in ["q", "quit"]:
        return
      else:
        print("Unrecognized Command. Use h/help")
