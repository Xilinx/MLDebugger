# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Project Overview

MLDebugger (`mldebug_xdp`) is a low-level debugger for the AI Engine (AIE)
inside AMD client NPUs. It is used by hardware/software engineers to
diagnose hangs and numerical mismatches in compiled VAIML designs.

A VAIML design is an ML model that has been compiled into a graph of
"layers". Each layer runs as one or more "kernels" (also called
"superkernels") on the AIE array. The debugger lets a user:

- Halt the AIE before any layer runs.
- Step layer-by-layer or iteration-by-iteration.
- Dump the contents of every input/weight/output buffer at each step
  (so the user can compare against a reference and find where numerics
  diverge).
- Read register and memory state when the chip is hung.
- Inspect saved core-dump files offline when no hardware is available.

Supported devices: Phoenix (`phx`), Strix (`stx`), Telluride
(`telluride`), NPU3 (`npu3`).

Backends:
- **XRT** -- talks to live hardware through XRT.
- **Test** -- a pure-Python simulator used by CI; no hardware needed.
- **Core Dump** -- read-only inspection of a previously captured dump.

## Key Concepts

These terms appear throughout the code; understanding them up front makes
the rest of the doc much shorter.

**Tile.** A single unit of the AIE grid, addressed by `(col, row)`. Three
flavors: *core tiles* (run code), *memory tiles* (shared SRAM, also
called L2), *shim tiles* (DMA into/out of host DDR).

**Buffer levels.**
- *L1* -- per-core scratch RAM, organized as ping/pong pairs so DMA and
  compute can overlap.
- *L2* -- memory-tile SRAM. May be split across columns when a buffer is
  larger than `MEM_TILE_SZ`.
- *L3* -- host DDR. Reached only via DMA through shim tiles.

**Layer / iteration.** The compiler turns the model into an ordered list
of layers. Each layer runs `num_iter` times. Within those iterations,
secondary counters (`depth_iter`, `buffer_iter`, `super_iter`,
`wts_iter`) say how often L1->L2 OFM transfers, L3->L2 IFM refills,
L2->L3 OFM spills, and weight reloads happen. Together these form the
"layer control parameters" (`Lcp`).

**Stamp.** A spatial replica of a small AIE region. A 4x4 design
"stamped" twice (`-o 2x4x4`) runs the same kernels in parallel on two
side-by-side 4x4 regions. Each stamp gets its own backend connection and
its own `AIEUtil` helper. The debugger schedules and breakpoints them
independently.

**Batch.** Conceptually the same as stamping but used for data-parallel
inference; multiple input samples processed in parallel by replicated
hardware. Detected from `device_batch_size` in `buffer_info.json`.

**Overlay.** The shape of the AIE region in use, written `NxCxR`
(stamps x columns x rows). Default `1x4x4`. Each stamp i occupies
columns `[i*C, (i+1)*C)`.

**PM reload.** The AIE has limited program memory; large designs split
their code across multiple ELFs and reload program memory between layer
groups. The debugger must know when this happens so it can re-arm
breakpoints in the new ELF before the core resumes.

**Breakpoint.** Hardware supports two PC breakpoints per core. The
debugger uses slot 0 for the start of a layer's kernel and slot 1 for
the end (the final lock-release instruction).

**Work directory.** The aiecompiler output, typically `Work/`. The
debugger reads:
- `aie/<col>_<row>[_reloadable<N>]/Release/*.lst` -- disassembly used
  to find function start/end PCs and lock releases.
- `aie/<col>_<row>[_reloadable<N>]/Release/*.map` -- to locate the
  `lcpPing`/`lcpPong` global variables.
- `ps/c_rts/aie_runtime_control.cpp` -- to map flexml layer ids to
  reloadable partitions (needed for PM-reload detection).
- `reports/mladf_compiler_report.json` -- optional, used to resolve
  templated-graph (TG) layer kernels.

**buffer_info.json.** The compiler's per-layer description of every
buffer (location, size, dtype, ping/pong addresses, L3 names) plus the
overlay layout and batch size. The single most important input to the
debugger after the work directory.

**FlexML runtime.** The runtime that actually invokes the model on the
NPU. The debugger does not run the model itself; it attaches to the
already-running FlexML process.

## Development Commands

### Environment & Build
```bash
uv venv && source .venv/bin/activate
uv pip install flake8 pytest pylint
uv build --wheel .
uv pip install --system "$(find dist -name '*.whl' | head -1)"
```

### Running

After install, the entry points are `mldebug` and `python -m mldebug`.

```bash
# Standalone AIE status (needs admin/root + an active HW context)
mldebug -s --device telluride -o 4x4

# VAIML batch debug -- dumps L1/L2 buffers per layer
mldebug -v path/to/vaiml_design_folder

# Interactive (GDB-like) VAIML debug
mldebug -v path/to/vaiml_design_folder -i

# Offline core-dump inspection (no HW needed)
mldebug -c path/to/coredump.bin -d telluride

# CI / no-hardware run
mldebug -x test -a ext/tests/vaiml -b ext/tests/vaiml/buffer_info.json -f skip_dump

# Just dump advanced AIE status to a file and exit
mldebug --dump-aie-status status.txt
```

Most users only need `-v`, `-i`, `-l`, `-o`, `-d`, `-c`, `-s`. The other
flags are developer-facing and hidden behind `ENABLE_DEV=1`.

Common runtime flags (`-f`):
- `skip_dump` -- run through the design without writing any buffers.
  Useful for timing or hang reproduction.
- `l2_ifm_dump` / `l1_ofm_dump` / `text_dump` -- adjust what gets dumped
  and the file format.
- `skip_iter` -- use a perf-counter trick to fast-forward iterations
  instead of polling per iteration.
- `multistamp` -- actually drive every stamp; default is to collapse to
  stamp 0 for sanity.

### Testing

```bash
cd ext && python run_tests.py   # batch + interactive flows on test backend
```

## High-Level Architecture

`ClientDebug` is the top-level handle and acts as a facade. On
construction it builds the parsed `LayerInfo`, the per-stamp backend
connections, the per-stamp `AIEUtil` helpers, an `AIEStatus` reader and
`MemoryDumper`, then wires them into a `BatchRunner` (execution engine)
and `InteractiveController` (interactive stepping). The CLI either calls
`execute_and_dump()` (batch mode) or hands the handle to
`InteractivePrompt` (interactive mode).

Information flow on a typical run:

1. **CLI / input parsing** decides which device, work dir, buffer_info
   file and run flags to use, and resolves the active VAIML subgraph.
2. **LayerInfo** parses `buffer_info.json`, builds an `Overlay`, then
   constructs one `Layer` per design layer. Each layer holds its
   buffers and its kernel "stamps".
3. **WorkDir** disassembles the per-stamp ELFs to learn each kernel's
   start PC, end PC and final lock-release PC. Those PCs are stored
   back into the `Layer.stamps` so the runner knows where to break.
4. **BatchRunner** arms PC breakpoints, lets the AIE run to the next
   breakpoint, dumps the relevant buffers, then arms the next layer's
   breakpoints. PM reloads are handled with combo-events so the
   breakpoint survives the program-memory swap.
5. **MemoryDumper** is what actually issues the reads through the
   backend and writes the results to disk in a documented binary format.
6. **AIEStatus** is used both during live debug (status command) and as
   part of the error report when the runner detects a hang.

The same primitives are reused for interactive mode -- `step`, `next`,
`continue` in the interactive prompt all eventually call the same
`run_layer` / `schedule_layer_start` methods on `BatchRunner`.

## Module Reference

Each section names the file, gives a one-line purpose, then lists the
points worth remembering.

### `mldebug_cli.py`, `__main__.py` -- CLI entry

Parses arguments, sets up logging, loops over VAIML subgraphs and
failsafe partitions, and creates one `ClientDebug` per partition.

- `--dump-aie-status` short-circuits everything -- it just writes a
  status snapshot and exits.
- Standalone mode (`-s`) is forced into interactive because there is no
  layer info to drive batch execution.
- Many advanced flags (`--peano`, `--unsupported_kernels`, `-auto`,
  `-b`) are hidden unless `ENABLE_DEV=1` is set in the environment.

### `input_parser.py` -- file/device/HW resolution

Resolves which work directory, buffer_info, etc. belong to the current
run, and verifies the host environment.

- `create_run_flags()` walks the VAIML folder layout
  (`<model>/vaiml_par_<N>/<fsp>/`) to find the right
  `aiecompiler/Work`, `buffer_info.json`, `flexmlrt-hsi.json` and
  `mladf_compiler_report.json`.
- `set_device()` auto-detects the device by reading the `HW_GEN`
  define from `aie_control.cpp`. Default is `telluride` on aarch64
  and `stx` elsewhere.
- `check_registry_keys()` (Windows only) ensures the IPU driver
  registry settings are correct. If it has to write keys it asks the
  user to reboot.
- `check_hw_context()` calls `xrt-smi` to discover running NPU
  contexts and asks the user to pick one if there are several.
- For multi-subgraph models, the user must name the active subgraph
  via `vitisai_config.json:passes[].vaiml_config.include_subgraphs`.

### `client_debug.py` -- top-level facade

Builds and owns the per-run state, then delegates execution and dumping
to the specialized components below. Adds a few "advanced"
standalone-only helpers (raw register read/write, manual PC breakpoints,
LCP read) that are exposed only in the advanced Python shell.

Note: the advanced/manual helpers operate only on the leftmost stamp.
Multi-stamp manual control is intentionally not supported -- doing it
safely needs the full scheduling logic in `BatchRunner`.

### `batch_runner.py` -- execution engine

This is where the real work happens. The two methods to read first are
`schedule_layer_start` and `run_layer`.

- `common_init()` runs once before any layer. If the user did not pass
  the `multistamp` flag, the runner collapses the design to a single
  stamp here -- it edits the layer/overlay/impls lists in place so the
  rest of the system simply sees a 1-stamp design. This is the safest
  default because multi-stamp scheduling is intricate.
- `schedule_layer_start()` arms the start (and optionally end) PC
  breakpoint on every stamp. When PM reload is expected it also
  installs a combo-event that survives the reload, *and* it may arm a
  future stamp's breakpoint *early* -- before the outer loop reaches
  the layer that stamp actually runs. This is necessary because if a
  stamp does not participate in the current layer, releasing it
  without a valid breakpoint would let it free-run past its real
  target. The "PM RELOAD on stamp X" log line is when arming happens,
  not when the reload physically occurs.
- `run_layer()` runs one layer to completion across all stamps using a
  thread pool, one worker per stamp.
- Inside a layer the runner alternates: continue, poll for breakpoint,
  identify whether we hit start or end PC, dump the appropriate
  buffers, increment the iteration counter, repeat.
- L3 buffer dumps for VAIML happen at the *last* iteration of a layer
  (the L3 OFM has been written by then).
- Hang detection: each breakpoint is polled up to 1200 times. After
  that the runner writes `aie_status_error.txt` and exits.

### `interactive_controller.py`, `interactive_prompt.py` -- interactive UI

`InteractiveController` adds `step_iteration`, `step_layer`,
`add_breakpoint` and `continue_execution` on top of `BatchRunner`. They
are disabled in `aie_only` mode because there are no layers to step
through.

`InteractivePrompt` provides two shells:
- A simple text shell with shortcuts (`s`, `n`, `b`, `c`, `i`, `a`,
  `d`, `l3`, `g`, `py`, `q`).
- A full Python REPL ("advanced shell"), entered by typing `py` or
  automatically for `--aie_only`. It exposes the raw debug helpers
  as local names (`status`, `rreg`, `wreg`, `pc_brkpt`, `goto_pc`,
  `funcs`, `calltree`, `dump_l3`, etc.) so power users can script
  arbitrary debug sequences.

### `debug_state.py` -- execution state

A small holder for "where are we now": current layer index, current
iteration, ping/pong toggle for OFM dumps, list of pending manual
breakpoints, and per-stamp PM-reload flags. `update_layer()` is the
generator the runner iterates to advance through the design.

### `layer_info.py` -- buffer / layer metadata

Parses `buffer_info.json` and produces the `Layer` and `Buffer` objects
the rest of the system uses.

- A `Layer` knows its kernels (one `Stamp` per AIE replica), its
  input/output/weight buffers, its L3 buffers, and its iteration
  counts (`Lcp`).
- A `Buffer` is the user-level concept (one IFM, one OFM, one weight
  set). Internally it holds an `L1Buffer` (ping/pong) and a list of
  `L2Buffer` chunks. Buffers larger than the memory-tile size are
  automatically split across columns at `MEM_TILE_SZ` boundaries.
- Layers that cannot be safely debugged -- concat layers, unsupported
  superkernels, templated graphs without an mladf report -- are
  flagged `is_unsupported` and dropped from the execution list. The
  list of unsupported superkernel names lives at module scope and can
  be extended at runtime via `--unsupported_kernels`.
- L3 offsets come from `flexmlrt-hsi.json`. Only a single parent
  spill buffer is supported today -- multi-DDR L3 is not yet handled.

### `work_dir.py` -- ELF disassembly

Walks the work directory, runs `llvm-objdump` (Peano) or parses
pre-generated `.lst` files (Chess), and extracts:
- Each function's start PC, end PC and final lock-release PC.
- Globals `lcpPing` / `lcpPong` from the map file (used to read
  layer control parameters at runtime).
- The flexml-id-to-reloadable-partition map from
  `aie_runtime_control.cpp` (used for PM-reload detection).

If Chess-style parsing fails the parser silently falls back to LLVM,
so most users never have to set `--peano` explicitly.

Some PCs are not safe to break on -- the parser checks for
`.nohwbrkpt` and `.aggressive_scheduled_block_id` directives in the
preceding lines and skips those.

### `memory_dumper.py` -- buffer dump I/O

Owns the on-disk layout and the actual reads. Files land under
`<output_dir>/batch<N>/layer_<order>/<buffer_type>/<col>_<row>/`.

Binary format: an 8-byte little-endian length header followed by the
raw 32-bit words. With `text_dump` the data is written as ASCII hex
instead.

L1 buffers are dumped from either the ping or the pong slot depending
on iteration parity. L2 OFM dumps shift the iteration index back by
one because OFM is dumped at the start of the *next* iteration.

L3 dumps are not direct reads -- they are forwarded to the FlexML
runtime via `DebugServer`, which writes them to disk on FlexML's side
and ACKs back. This is necessary because L3 lives in host DDR and
only FlexML knows how to access it.

### `debug_server.py` -- FlexML coordination

A tiny TCP server on `127.0.0.1:9000` that waits for one connection
from the FlexML runtime. Wire format is intentionally trivial so the
runtime can implement it in C++: a 512-byte null-padded filename
followed by 4-byte little-endian offset and size; FlexML writes the
buffer to that path and sends an ACK. Used only when `-l3` or
`--automated_debug` is passed; FlexML must be built with the matching
protocol and run with `ENABLE_ML_DEBUG=2` (or `=3` for fully
automated mode where the debugger drives FlexML through the run).

### `aie_status.py`, `extra/aie_guidance.py` -- status snapshot

`AIEStatus.get()` reads core/memory/shim status, DMA channel state,
locks and event registers and prints (or writes) a tile-grid
visualization. With `advanced=True` it includes per-BD and per-lock
detail. With `guidance=True` it also runs `AIEGuidanceChecker` which
applies a JSON-defined set of pass/fail rules and reports
`ERROR/WARNING/INFO`. `get_uc_status()` exists for Telluride only,
since only AIE2PS has the UC module.

### `aie_util.py` -- per-stamp AIE helpers

High-level operations that touch every core in a stamp: bulk register
read/write, performance-counter read, LCP read, error scan, FSP
breakpoint, combo-event setup for PM reload.

Two non-obvious pieces:
- The "skip iterations" mechanism arms perf counter 1 to count
  `PC_0_CORE` events and lets the AIE run until the counter hits the
  target. This is much faster than handling every iteration with a
  Python round-trip but trusts that all tiles advance together (only
  one tile is polled, with a 10 s timeout).
- `check_errors()` reads the per-tile error event register(s) -- two
  registers on NPU3, one elsewhere -- and sets a latch so the same
  errors are not reported repeatedly.

### `aie_overlay.py` -- overlay geometry

Parses `NxCxR` from `-o` (or the layout in `buffer_info.json`, which
is stored as `[stamps, nrow, ncol]` rather than `NxCxR`) and builds
the list of `(col, row)` tiles per stamp. Methods like `get_tiles`
filter that list by tile type, with the AIE row offset already added.

### `arch/` -- per-device definitions

`loader.load_aie_arch(device)` returns one of `aie2p_defs` (PHX/STX),
`aie2ps_defs` (Telluride), or `npu3_defs`. Each module exposes the
same surface: tile-type constants, register-name dictionaries
(`Core_registers`, `Memory_tile_registers`, `Shim_tile_registers`),
DMA-BD layout, event tables, register parsers, and capability flags
like `HAS_UC_MODULE` (TEL only) and `HAS_PER_CHANNEL_BD_REGS`
(NPU3 mem-tile only). New devices are added by writing one more
`*_defs.py` and adding it to the loader.

### `backend/` -- HW abstraction

`BackendInterface` is the ABC every backend implements. The factory
does lazy imports so missing optional dependencies (XRT,
pybind module) only fail when actually requested.

- `xrt_impl.py` wraps the C++ pybind module built from `cpp/`. Memory
  reads are chunked at 4 KB; large dumps therefore translate into
  many XRT calls.
- `test_impl.py` is a pure-Python simulator. It walks layer
  iterations, can inject a fake hang at a random layer when the
  `mock_hang` flag is set, and is what the CI suite runs against.
- `core_dump_impl.py` reads from a saved core-dump file. Per-device
  geometry (rows, columns, per-tile block size) is hard-coded in
  `DEVICE_CONFIGS`. There is a pure-Python fallback for systems
  where the C++ backend is not built. All write/continue/breakpoint
  operations are no-ops on this backend.

### `mladf_report.py` -- templated-graph mapping

Reads `mladf_compiler_report.json` and maps each `buffer_info` layer
to its aiecompiler layer(s) by matching parent graph names
(`templated_graph_*`, `flexml_layers[N]`). This is what allows the
debugger to handle templated-graph (TG) layers when `enable_tg` is
on; without this report the debugger marks TG layers as unsupported
and skips them.

### `utils.py` and friends

Cross-cutting helpers: the global `LOGGER`, a tiny `Version` class, a
`timeit` decorator, the `print_tile_grid` formatter, and platform
checks (`is_windows`, `is_linux`, `is_aarch64`).

`bin/` ships precompiled `llvm-objdump` and `c++filt` for both
Windows and Linux, plus the `initial_halt_elfs/` used to halt the
AIE at boot on Telluride. `cpp/` holds the pybind11 sources for the
XRT backend. `extra/calltree.py` is a standalone Peano LST parser
that produces an indented call tree for a given stamp.

## Important Constraints and Assumptions

- Python 3.10 on Windows / x86 Linux; Python 3.12 on embedded
  aarch64 (Telluride).
- 2-space indentation everywhere (enforced by ruff and pylint).
- The XRT backend on Windows requires registry keys and an
  Administrator terminal. On Linux use a root shell -- `sudo` does
  not work for the operations the backend performs. On x86 Linux you
  also need `sudo modprobe amdxdna timeout_in_sec=0` to keep the NPU
  alive long enough to debug.
- On Telluride add `[Runtime] cert_timeout_ms = 999999` to `xrt.ini`.
- The application under debug must keep its `hw_context` alive while
  the debugger is attached. The conventional way is to set
  `ENABLE_ML_DEBUG=1`, which makes the FlexML host code wait for
  user input after the kernel hangs.
- L3 buffer dumping requires FlexML to be built with the matching
  `DebugServer` protocol and to be launched with `ENABLE_ML_DEBUG=2`
  (or `=3` for fully automated mode).
- Hardware exposes only two PC breakpoint slots per core, so the
  debugger uses slot 0 for "start of layer" and slot 1 for "end of
  layer".
- Some superkernels are known to misbehave with breakpoints
  (`superkernel_silu1d`, `mha_adf_wrapper`, `resize_adf_wrapper`,
  ...) and are flagged unsupported. Likewise a handful of kernels
  with multiple lock-release sites have the end-PC breakpoint
  skipped.
- Manual / advanced register and breakpoint control acts only on the
  leftmost stamp; multi-stamp manual control is intentionally not
  exposed.
- The "skip iterations" optimization polls a single tile and assumes
  the rest advanced in lockstep; failures fall back to a 10 s
  timeout and a hang report.
- Concat layers and templated-graph layers without a matching mladf
  report are silently dropped from the execution list.

## VAIML Subgraphs and Failsafe Partitions

A real VAIML compile can produce more than one independent piece of
work. There are two such mechanisms.

**Subgraphs** are the supported, modern split. The compiler partitions
the model into independent compute graphs early, each in its own
folder under `vaiml_folder/<model>/vaiml_par_<N>/`. The debugger debugs
one subgraph at a time. If the model has more than one subgraph the
user must name the one to debug in `vitisai_config.json` under
`passes[].vaiml_config.include_subgraphs`.

**Failsafe partitions (FSP)** are an older mechanism, deprecated but
still supported. They split a single subgraph into multiple
program-memory partitions for very large models. Each FSP has its own
work directory and `buffer_info.json`. Their execution order comes from
`partition-info.json`. The debugger runs `debug()` once per FSP,
prompting the user (or coordinating with FlexML) to advance between
partitions.

```
vaiml_folder/<model>/vaiml_par_0/
  partition-info.json
  0/aiecompiler/Work/
  0/buffer_info.json
  1/aiecompiler/Work/
  1/buffer_info.json
```

## Hang and Mismatch Quick Reference

A core is almost certainly hung if any of the following is true: any
core is in `ERROR_HALT` or in reset; every core is in `LOCK_STALL`;
a `DM_ADDRESS_OUT_OF_RANGE_CORE` event is set.

A numerical mismatch is likely if any of the floating-point error
events are set (`FP_HUGE/OVERFLOW_CORE`, `FP_ZERO/UNDERFLOW_CORE`,
`FP_INVALID_CORE`, `FP_DIV_BY_ZERO_CORE`), the integer scaling events
are set (`SRS_OVERFLOW`, `UPS_OVERFLOW`), or any error bits are set
in SR1/SR2.

To localize a control-code hang to a specific source line, recompile
the design with `--multi-layer-record-txn` and then use the
`control_instr()` helper in the advanced shell. It reads the
memory-tile spare register, which the controller writes a unique
integer into before each transaction.

# Coding Guidelines (Inspired from Karpathy)

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
