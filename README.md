## MLDebugger

### Introduction

MLDebugger is a tool to provide visibility into NPU hardware. 
It supports Client on Windows and Telluride with some support for Linux.
The tool has to be run inside the debug target.  

#### Feature Summary

Device Support: Phx, Stx, Telluride, npu3

##### Standalone mode
1. AIE Status, UC Status
2. Use AIE Breakpoints, Single Step at overlay level
3. Peano design lst dump
4. Function calltree and function list for all reloadables
5. Read/Write registers, memory

##### VAIML Batch Debug
1. L1, L2, L3 Dump for each Layer in design for mismatch analysis
2. Multi stamp, batch support, X2 support (TG support upcoming)
3. Status at Hang and at each layer
4. Error Detection

##### VAIML Interactive Debug
1. GDB Like Interface to step through and inspect layers
2. Design Inspection
3. Advanced mode with all Standalone functionality and per layer inspection

##### XRT CoreDump Analysis
All read features of Standalone mode like status, read_registers


#### Coredump Instructions
```
# Clone Debugger
> python mldebug.py -c <npu_coredump*> -d telluride
> python mldebug.py -c <npu_coredump*> -d npu3
```

### Setup Requirements

#### General
1. Active Hardware Context (Debug Target). If it's a hang testcase, create env variable: "ENABLE_ML_DEBUG=1". 
This will keep the hostcode alive if running with OnnxRuntime. An input() wait in python also works. 
For custom hostcode, make sure the hw_context object is alive after encountering XRT kernel hang.
2. xrt-smi in PATH
3. Secondary Root/Administrator terminal to launch Debugger (sudo doesn't work on Linux)
####  Telluride
1. Python=3.12
2. Following xrt.ini setting to keep the NPU Alive for Debug:
```
[Runtime]
cert_timeout_ms = 999999
```
####  Client/Windows
1. Python=3.10
2. Registry keys : First run of tool will automatically create required registry keys.
####  x86 Linux
1. Python=3.10
2. Following Driver setup to keep NPU Alive for Debug:
```
sudo rmmod amdxdna
sudo modprobe amdxdna timeout_in_sec=0
```

### NPU Status Dump

Status Dump provides snapshot of AIE Array State in case of a hang.
MLDebugger's Standalone mode (-s) is used for this purpose
Supported devices : "phx", "stx", "telluride"
Default overlay is 4x4

1. Start Application and wait for it to hang. Additionally, ENABLE_ML_DEBUG can be used to keep the hw_ctx alive
2. Get AIE Status
Example Usage:
```
> mldebug -s -d "telluride"
# A terminal will appear with a list of commands
# Print AIE Status on console
> status()
# Write advanced status to a file 
> status(filename="status.txt", advanced=True)
```

### NPU Control Code Hang Debug

This feature can find source code lines where controller is stuck in case of a hang.

1. add "--multi-layer-record-txn" to aiecompiler options and recompile. Then open Work/ps/c_rts/aie_runtime_control.cpp. You should see writes to spare registers in form of:  "adf::write32b(adf::memory_tile.., <Integer>)". The Integer value will keep changing for each write. As the controller executes, it writes this value to the spare register. At runtime, MLDebugger can read the spare register to find last adf::write32b that was executed.


2. Start Application and wait for it to hang. Additionally, ENABLE_ML_DEBUG can be used to keep the hw_ctx alive
3. Start MLDebugger in standalone mode. For example: mldebug -s -d "telluride"
4. Use "control_instr()" command to get the spare register value.

### Debugging a VAIML Application Layer by Layer

#### Telluride
1. Add initial Halt to VAIML Application. Find out location of initial halt elfs:
```
python3 -c "import mldebug;from pathlib import Path; print(Path(mldebug.__file__).parent)"
# Under this directory, look for "initial_halt_elfs/telluride" directory.
NOTE: For git users, the halts can be found here: MLDebug/ext/initial_halt_elfs/telluride
```
2. The initial halt elfs are of format <stamps>x<row>x<col>. Copy the elf that matches overlay to the design directory (for example: 4x4, will use 1x4x4)
3. Add following content to xrt.ini:
```
[Debug]
aie_halt=true
[AIE_halt_settings]
control_code=aieHalt1x4x4.elf # Put your elfname here
```
4. Set Env Variables. See FAQ for examples. <br />
 a. Before running the application, set environment variable "ENABLE_ML_DEBUG=1". <br />
 b. Set "AMD_NPU_SDK_PATH" to location of xrt_package corresponding to the driver. <br />
5. Run Host code and wait till a message appears : "Running in Debug mode"
6. Launch the debugger. Some examples are give below:
```
mldebug -v ./resnet18
mldebug  -b ./resnet18/buffer_info.json -a ./resnet18/Work -l dumps -o 4x4
```
#### Client Windows
1. Add Halt to VAIML Application. Add following content to xrt.ini:
```
[Debug]  
aie_halt=true
```
2. Before running the application, set ENABLE_ML_DEBUG=1
3. Run Host code and wait till a message appears : "Running in Debug mode"
```
## Launch the debugger. Some examples are give below:
mldebug -v <top level vaiml_design_folder>
mldebug  -b .\resnet18\buffer_info.json -a .\resnet18\Work -l dumps -o 4x4
```

### User Options

```
usage: mldebug [-h] [-b <file>] [-a <file>] [-d DEV] [-o <cxr>] [-i] [-l <dir>] [-v <path>] [-s] [-l3] [-f [<flag1>, <flag2> ...]]

 AIE Debug for VAIML Requirements:
  1. Active Hardware Context (xrt-smi in PATH)
  2. Run as Root/admin
Default mode: Standalone (-s).
Binary dumps have 8byte header specifying total bytes.

options:
  -h, --help            show this help message and exit
  -a <file>, --aie_dir <file>
                        Path to AIE Work Directory. Default: Work
  -c COREDUMP_FILE, --core_dump COREDUMP_FILE
                        Run standalone mode for core-dump inspection.
                        Use -d flag to specify device.
  --dump-aie-status <output_file_name>
                        Write AIE status to a file and exit.
  --no_header           Assume raw core dump without header. Use with -c.
  -d DEV, --device DEV  AIE device [phx,stx,telluride]. Default: telluride
  -o <cxr>, --overlay <cxr>
                        Overlay used by design. Default: 4x4
  -i, --interactive     Launch in Interactive Mode. Default: Batch
  -l <dir>, --output_dir <dir>
                        Directory to store memory and status dumps. Default: layer_dump
  -v <path>, --vaiml_folder_path <path>
                        Specify the VAIML top level folder path.
  -s, --aie_only        Standalone AIE debug. Work dir can be optionally specified.
  -l3, --l3             Dumps L3 buffers during the execution
  -f [<flag1>, <flag2> ...], --run_flags [<flag1>, <flag2> ...]
                        Specify one or more runtime flags for batch mode:
                        skip_dump       : Do not dump memory
                        l2_ifm_dump     : Dump only L2 IFM buffers
                        skip_iter       : Skip iterations in batch mode when possible
                        dump_temps      : Write intermediate (.lst) files to disk
```

### FAQ

#### Hang Scenarios
1. Any AIE cores in ERROR_HALT
2. All AIE cores in LOCK_STALL
3. Any AIE cores in Reset
4. DM_ADDRESS_OUT_OF_RANGE_CORE is present in events

#### Mismatch Scenarios

1. Any of the following events are present: "FP_HUGE/OVERFLOW_CORE", "FP_ZERO/UNDERFLOW_CORE",
"FP_INVALID_CORE","FP_DIV_BY_ZERO_CORE", "SRS_OVERFLOW", "UPS_OVERFLOW".
2. Error bits in SR1/SR2 registers are set

#### FAQ
1. Hang in conv2d_maxpool: Is caused due to an aiecompiler optimization that cause race condition sometimes. Fix is to rerun aiecompiler with "--multi-layer-lcp-acquire-lock" switch
2. No L3 dumps for TG layers. This is due to L3 placements and work in progress.
3. FlexmlRT doesn't halt or env variables can't be set. Use correct command for the shell: 
 ```
 # Powershell
 $Env:ENABLE_ML_DEBUG=1
 # CMD
 set ENABLE_ML_DEBUG=1
 # Linux
 export ENABLE_ML_DEBUG=1
 ```
