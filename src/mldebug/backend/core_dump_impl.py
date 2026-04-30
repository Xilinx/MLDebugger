# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Core Dump Backend - Read-only backend for analyzing core dumps
"""

import struct
from pathlib import Path
from mldebug.utils import print_tile_grid
from mldebug.arch import AIE_DEV_PHX, AIE_DEV_STX, AIE_DEV_TEL, AIE_DEV_NPU3
from .backend_interface import BackendInterface

try:
  from .xrt_backend import MlDebug
  HAS_XRT_BACKEND = True
except ImportError:
  HAS_XRT_BACKEND = False

# Device architecture metadata (from C++ CoreDumpDataAccessBackend)
DEVICE_CONFIGS = {
  AIE_DEV_PHX: {
    "hwGen": 2,
    "baseAddr": 0x0,
    "core_row_start": 2,
    "mem_row_start": 1,
    "memtile_rows": 1,
    "numrows": 6,
    "numcols": 4,
    "shim_tile_block_size": 1024 * 1024,  # 1MB
    "mem_tile_block_size": 1024 * 1024,   # 1MB
    "core_tile_block_size": 1024 * 1024,  # 1MB
  },
  AIE_DEV_STX: {
    "hwGen": 4,
    "baseAddr": 0x0,
    "core_row_start": 2,
    "mem_row_start": 1,
    "memtile_rows": 1,
    "numrows": 6,
    "numcols": 8,
    "shim_tile_block_size": 1024 * 1024,
    "mem_tile_block_size": 1024 * 1024,
    "core_tile_block_size": 1024 * 1024,
  },
  AIE_DEV_TEL: {
    "hwGen": 5,
    "baseAddr": 0x0,
    "core_row_start": 3,
    "mem_row_start": 1,
    "memtile_rows": 2,
    "numrows": 7,
    "numcols": 36,
    "shim_tile_block_size": 1024 * 1024,
    "mem_tile_block_size": 1024 * 1024,
    "core_tile_block_size": 1024 * 1024,
  },
}

class CoreDumpFallbackReader:
  """
  Pure Python fallback implementation for reading core dump files.
  Replicates the C++ CoreDumpDataAccessBackend logic.
  """
  def __init__(self, core_dump_file, dev_name, no_header=False):
    """
    Initialize the fallback reader

    Args:
      core_dump_file (str): Path to the binary core dump file
      dev_name (str): Device name (phx, stx, telluride, npu3)
      no_header (bool): If True, skip header parsing and treat data as starting at offset 0
    """
    self.filename = core_dump_file
    self.dev_name = dev_name.lower()
    self.file_handle = None

    # Without a header to parse, we have no way to recover from an unknown device name.
    # With a header, _parse_header() will detect/override dev_name and metadata.
    if no_header and self.dev_name not in DEVICE_CONFIGS:
      raise ValueError(f"Unknown device: {dev_name}. Supported: {list(DEVICE_CONFIGS.keys())}")

    # Provisional metadata; may be replaced by _parse_header() based on header hwGen.
    self.metadata = DEVICE_CONFIGS.get(self.dev_name)
    self.header_size = 256  # Default header size

    # Open the binary dump file
    if not Path(self.filename).exists():
      raise FileNotFoundError(f"Core dump file not found: {self.filename}")

    try:
      self.file_handle = open(self.filename, "rb")
    except PermissionError as e:
      raise PermissionError(f"Permission denied: Cannot open core dump file '{self.filename}'."
                            " Check file permissions.") from e
    except OSError as e:
      raise OSError(f"Failed to open core dump file '{self.filename}': {e}") from e
    except Exception as e:
      raise RuntimeError(f"Unexpected error opening core dump file '{self.filename}': {e}") from e

    if no_header:
      self.header_size = 0
      print("[INFO] Skipping header parsing; treating core dump data as starting at offset 0")
    else:
      # Read and parse the header
      try:
        self._parse_header()
      except Exception as e:
        # Close file handle if header parsing fails critically
        if self.file_handle:
          self.file_handle.close()
          self.file_handle = None
        raise RuntimeError(f"Failed to initialize core dump reader: {e}") from e

  def __del__(self):
    """Close file handle on cleanup"""
    if self.file_handle:
      self.file_handle.close()

  def _parse_header(self):
    """
    Parse the core dump file header.
    Header structure (from C++ coreDumpHeader):
      - char magicNumber[4]: "NPU" (4 bytes)
      - uint32_t versionNum: Version number (4 bytes)
      - uint32_t headerSize: Actual header size in bytes (4 bytes)
      - uint8_t hwGen: Hardware generation (1 byte)
      - uint8_t coreRowStart: Core row start (1 byte)
      - uint8_t memRowStart: Memory row start (1 byte)
      - uint8_t memTileRows: Number of memory tile rows (1 byte)
      - uint8_t totalNumRows: Total number of rows (1 byte)
      - uint8_t totalNumCols: Total number of columns (1 byte)
    """
    assert self.file_handle is not None
    try:
      self.file_handle.seek(0)

      # Read magic number (4 bytes)
      magic = self.file_handle.read(4)
      if len(magic) != 4:
        raise RuntimeError("Core dump file is too small or corrupted: cannot read header magic number")

      magic_str = magic.decode('ascii', errors='ignore').rstrip('\x00')
      if magic_str != "NPU":
        raise ValueError(f"Invalid core dump file format: expected magic number 'NPU', got '{magic_str}'")

      # Read version number (4 bytes, little-endian uint32)
      version_data = self.file_handle.read(4)
      if len(version_data) != 4:
        raise RuntimeError("Core dump file is corrupted: cannot read version number")
      version_num = struct.unpack("<I", version_data)[0]

      # Read header size (4 bytes, little-endian uint32)
      header_size_data = self.file_handle.read(4)
      if len(header_size_data) != 4:
        raise RuntimeError("Core dump file is corrupted: cannot read header size")
      self.header_size = struct.unpack("<I", header_size_data)[0]

      # Validate header size is reasonable
      if self.header_size < 18 or self.header_size > 1024 * 1024:  # Between 18 bytes and 1MB
        raise ValueError(f"Invalid header size in core dump: {self.header_size} bytes (expected 18-1048576)")

      # Read device metadata (6 bytes)
      metadata_data = self.file_handle.read(6)
      if len(metadata_data) != 6:
        raise RuntimeError("Core dump file is corrupted: cannot read device metadata")

      hw_gen, core_row_start, mem_row_start, mem_tile_rows, total_rows, total_cols = struct.unpack("<BBBBBB", metadata_data)

      # Detect device from header hwGen and override dev_name/metadata
      detected_dev = None
      for name, cfg in DEVICE_CONFIGS.items():
        if cfg["hwGen"] == hw_gen:
          detected_dev = name
          break

      if detected_dev is None:
        raise ValueError(f"Unknown hwGen {hw_gen} in core dump header; supported: "
                         f"{[(n, c['hwGen']) for n, c in DEVICE_CONFIGS.items()]}")

      if detected_dev != self.dev_name:
        print(f"[INFO] Device detected from core dump header: \'{detected_dev}\'; overriding '{self.dev_name}'")
      else:
        print(f"[INFO] Device detected from core dump header: '{detected_dev}'")

      self.dev_name = detected_dev
      self.metadata = DEVICE_CONFIGS[detected_dev]

      expected_core_row_start = self.metadata["core_row_start"]
      expected_mem_row_start = self.metadata["mem_row_start"]
      expected_mem_tile_rows = self.metadata["memtile_rows"]
      expected_rows = self.metadata["numrows"]
      expected_cols = self.metadata["numcols"]

      print( "[INFO] Core dump header:")
      print(f"  Magic: {magic_str}")
      print(f"  Version: {version_num}")
      print(f"  Header size: {self.header_size} bytes")
      #print(f"  HW Gen: {hw_gen} (device: {self.dev_name})")
      print(f"  Core row start: {expected_core_row_start}")
      print(f"  Mem row start: {expected_mem_row_start}")
      print(f"  Mem tile rows: {expected_mem_tile_rows}")
      print(f"  Total rows:  {expected_rows}")
      print(f"  Total cols: {expected_cols}")

    except (ValueError, RuntimeError) as e:
      # Re-raise critical errors that indicate file corruption or format issues
      raise ValueError("I/O error while reading core dump header") from e
    except OSError as e:
      raise RuntimeError("I/O error while reading core dump header") from e
    except Exception as e:
      raise RuntimeError("Unexpected error parsing core dump header") from e

  def _calculate_file_position(self, col, row, offset):
    """
    Calculate the file position for a given tile and register offset.
    Replicates the C++ logic from CoreDumpDataAccessBackend::readRegister

    Args:
      col (int): Column index
      row (int): Row index
      offset (int): Register offset within the tile

    Returns:
      int: File position in bytes
    """
    shim_tile_block_size = self.metadata["shim_tile_block_size"]
    mem_tile_block_size = self.metadata["mem_tile_block_size"]
    core_tile_block_size = self.metadata["core_tile_block_size"]
    memtile_rows = self.metadata["memtile_rows"]
    core_row_start = self.metadata["core_row_start"]

    # Calculate tower size (one column's worth of tiles)
    tower_size = (shim_tile_block_size +
                  mem_tile_block_size * memtile_rows +
                  core_tile_block_size * (self.metadata["numrows"] - core_row_start))

    # Calculate tile position index based on row type
    if row == 0:
      # Shim tile
      tile_pos_index = col * tower_size
    elif 0 < row <= memtile_rows:
      # Memory tile
      tile_pos_index = col * tower_size + shim_tile_block_size + (row - 1) * mem_tile_block_size
    else:
      # Core tile
      tile_pos_index = (col * tower_size +
                        shim_tile_block_size +
                        mem_tile_block_size * memtile_rows +
                        (row - 1 - memtile_rows) * core_tile_block_size)

    file_position = self.header_size + tile_pos_index + offset
    return file_position

  def read_register(self, col, row, offset):
    """
    Read a 32-bit register from the core dump file

    Args:
      col (int): Column index
      row (int): Row index
      offset (int): Register offset

    Returns:
      int: 32-bit register value
    """
    assert self.file_handle is not None
    # Validate inputs
    if col >= self.metadata["numcols"]:
      raise ValueError(f"Column {col} out of range (max: {self.metadata['numcols'] - 1})")
    if row >= self.metadata["numrows"]:
      raise ValueError(f"Row {row} out of range (max: {self.metadata['numrows'] - 1})")

    try:
      file_position = self._calculate_file_position(col, row, offset)

      self.file_handle.seek(file_position)
      data = self.file_handle.read(4)

      if len(data) != 4:
        raise RuntimeError(f"Failed to read 4 bytes at position {file_position}")

      # Unpack as little-endian uint32
      value = struct.unpack("<I", data)[0]
      return value

    except Exception as e:
      print(f"[ERROR] Failed to read register at col={col}, row={row}, offset=0x{offset:x}: {e}")
      return 0

  def dump_buffer(self, col, row, offset, size):
    """
    Read a buffer from memory (L1/L2) and return as list of 32-bit words

    Args:
      col (int): Column index
      row (int): Row index
      offset (int): Memory offset
      size (int): Size in bytes

    Returns:
      list[int]: List of 32-bit words
    """
    assert self.file_handle is not None
    if size % 4 != 0:
      print(f"[WARNING] Buffer size {size} is not 4-byte aligned, rounding down")
      size = (size // 4) * 4

    num_words = size // 4
    result = []

    try:
      file_position = self._calculate_file_position(col, row, offset)
      self.file_handle.seek(file_position)
      data = self.file_handle.read(size)

      if len(data) != size:
        raise RuntimeError(f"Failed to read {size} bytes at position {file_position}")

      # Unpack as little-endian uint32 array
      for i in range(num_words):
        word = struct.unpack("<I", data[i*4:(i+1)*4])[0]
        result.append(word)

      return result

    except Exception as e:
      print(f"[ERROR] Failed to dump buffer at col={col}, row={row}, offset=0x{offset:x}, size={size}: {e}")
      return [0] * num_words


class CoreDumpImpl(BackendInterface):
  """
  Core Dump Backend - Provides read-only access to core dumps.
  All write/control methods print warnings and return without action.
  """

  is_offline = True
  def __init__(self, aie_overlay_tiles, ctx_id, pid, dev_name, core_dump_file=None, no_header=False) -> None:
    """
    Initialize the Core Dump backend

    Args:
      aie_overlay_tiles: List of AIE core tiles
      ctx_id: Context ID (unused in core dump mode)
      pid: Process ID (unused in core dump mode)
      dev_name: Device name (phx, stx, telluride, npu3)
      core_dump_file: Path to core dump file (required)
      no_header: If True, parse core dump assuming no header (data starts at offset 0).
                 Forces use of the Python fallback reader.
    """
    self.overlay_aie_core_tiles = aie_overlay_tiles
    self.pc_brkpts = [0, 0]
    self.binding = None
    self.fallback_reader = None
    self.use_fallback = False

    if not core_dump_file:
      raise ValueError("core_dump_file is required for CoreDumpImpl backend")

    if no_header or not HAS_XRT_BACKEND:
      # Python fallback reader is required to support headerless parsing
      #print("[INFO] --no_header specified: using Python fallback reader (C++ binding does not support headerless mode)")
      self.use_fallback = True
    else:
      # Try to initialize the C++ binding first
      try:
        self.binding = MlDebug(list(self.overlay_aie_core_tiles), ctx_id, pid, dev_name, "debuglibrary", core_dump_file)
        print("[INFO] Core Dump backend initialized with C++ DebugLibrary")
      except ImportError:
        self.use_fallback = True

    if self.use_fallback:
      self.fallback_reader = CoreDumpFallbackReader(core_dump_file, dev_name, no_header=no_header)

    print("[INFO] Core Dump backend is read-only. Write/control operations will be ignored.")

  def read_core_debug_status(self):
    """
    Reads the core debug status
    """
    if self.use_fallback:
      print("[WARNING] read_core_debug_status() not fully supported in fallback mode")
      return
    status = self.binding.read_core_debug_status()
    print_tile_grid("Core Debug Status", self.overlay_aie_core_tiles, status)

  def read_core_execution_status(self):
    """
    Reads the core status status
    """
    if self.use_fallback:
      print("[WARNING] read_core_execution_status() not fully supported in fallback mode")
      return
    status = self.binding.read_core_execution_status()
    print_tile_grid("Core Execution Status", self.overlay_aie_core_tiles, status)

  def poll_core_status(self):
    """
    Polls the core debug status. Returns 1 if core is halted
    """
    return 1

  def configure_performance_counters(self):
    """
    Configure performance counters - NOT SUPPORTED in core dump mode
    """
    print("[WARNING] configure_performance_counters() is not supported in core dump mode (read-only)")

  def set_performance_counter_halt(self):
    """
    Set performance counter halt - NOT SUPPORTED in core dump mode
    """
    print("[WARNING] set_performance_counter_halt() is not supported in core dump mode (read-only)")

  def read_core_pc(self, all_tiles=False):
    """
    Reads the current core Program Counter line
    """
    if self.use_fallback:
      print("[WARNING] read_core_pc() not fully supported in fallback mode")
      if all_tiles:
        return [0] * len(self.overlay_aie_core_tiles)
      return 0
    if all_tiles:
      return self.binding.read_core_pc()
    return self.binding.read_core_pc()[0]

  def read_all_core_pc(self):
    """
    Reads the current core Program Counter for all tiles from core dump
    """
    if self.use_fallback:
      print("[WARNING] read_all_core_pc() not fully supported in fallback mode")
      return
    pc = self.read_core_pc(all_tiles=True)
    print_tile_grid("Core PC", self.overlay_aie_core_tiles, register_values=pc, format_type="int")

  def continue_aie(self):
    """
    Un-halts the AIE and resumes execution - NOT SUPPORTED in core dump mode
    """
    print("[WARNING] continue_aie() is not supported in core dump mode (read-only)")

  def set_pc_breakpoint(self, pc_value, idx=0):
    """
    Sets a PC breakpoint - NOT SUPPORTED in core dump mode
    """
    print("[WARNING] set_pc_breakpoint is not supported in core dump mode (read-only)")

  def clear_pc_breakpoint(self, idx=0):
    """
    Clears a PC breakpoint - NOT SUPPORTED in core dump mode
    """
    print("[WARNING] clear_pc_breakpoint is not supported in core dump mode (read-only)")

  def print_pc_breakpoints(self):
    """
    Print currently configured pc events
    """
    print(f"Currently configured PC Breakpoints: {self.pc_brkpts}")

  def enable_pc_halt(self):
    """
    Enable PC halt - NOT SUPPORTED in core dump mode
    """
    print("[WARNING] enable_pc_halt() is not supported in core dump mode (read-only)")

  def get_pc(self):
    """
    Reads the current PC Value from a tile
    """
    if self.use_fallback:
      print("[WARNING] get_pc() not fully supported in fallback mode")
      return [0] * len(self.overlay_aie_core_tiles)
    return self.binding.get_pc()

  def disable_pc_halt(self):
    """
    Disable PC halt - NOT SUPPORTED in core dump mode
    """
    print("[WARNING] disable_pc_halt() is not supported in core dump mode (read-only)")

  def dump_memory(self, c, r, buffer_offset, buffer_size, filename=None):
    """
    Read and return L1/L2 memory as a list
    c (int): column
    r (int): row
    buffer_offset (int): memory offset
    buffer_size (int): size of memor to read in bytes
    filename (str): optionally dump the memory to a file
    """
    chunk_sz = 0x1000
    chunks = []
    num_chunks = buffer_size // chunk_sz
    remainder = buffer_size % chunk_sz

    if self.use_fallback:
      # Use fallback reader
      for i in range(num_chunks):
        offset = buffer_offset + i * chunk_sz
        chunk = self.fallback_reader.dump_buffer(c, r, offset, chunk_sz)
        chunks.extend(chunk)

      if remainder > 0:
        offset = buffer_offset + num_chunks * chunk_sz
        chunk = self.fallback_reader.dump_buffer(c, r, offset, remainder)
        chunks.extend(chunk)
    else:
      # Use C++ binding
      for i in range(num_chunks):
        offset = buffer_offset + i * chunk_sz
        chunk = self.binding.dump_buffer(c, r, offset, chunk_sz)
        chunks.extend(chunk)

      if remainder > 0:
        offset = buffer_offset + num_chunks * chunk_sz
        chunk = self.binding.dump_buffer(c, r, offset, remainder)
        chunks.extend(chunk)

    if filename:
      with open(filename, "w", newline="\n", encoding="utf-8") as file:
        for word in chunks:
          file.write(f"{word:08x}\n")
      print("Memory dumped to file: ", filename)
      return

    return chunks

  def read_register(self, c, r, reg):
    """
    Reads a register from a tile
    c (int): column
    r (int): row
    reg (int): register offset
    """
    if self.use_fallback:
      return self.fallback_reader.read_register(c, r, reg)
    return self.binding.read_register(c, r, reg)

  def print_register(self, c, r, reg):
    """
    read and display register for humans
    c (int): column
    r (int): row
    reg (int): register offset
    """
    if self.use_fallback:
      value = self.fallback_reader.read_register(c, r, reg)
    else:
      value = self.binding.read_register(c, r, reg)

    print(f"Hex:     0x{hex(value)[2:].upper():>02}")
    print(f"Decimal: {value}")
    binary = f"{value & 0xFFFFFFFF:032b}"
    # Insert spaces every 4 bits
    spaced_binary = " ".join(binary[i : i + 4] for i in range(0, 32, 4))
    # Create bit position indicators
    pos_tens = "3322 2222 2222 1111 1111 11"
    pos_ones = "1098 7654 3210 9876 5432 1098 7654 3210"
    underline = "-" * len(spaced_binary)

    print(f"Binary: \n{pos_tens}\n{pos_ones}\n{underline}\n{spaced_binary}")

  def write_register(self, c, r, reg, value):
    """
    Writes to a register - NOT SUPPORTED in core dump mode
    """
    print("[WARNING] write_register is not supported in core dump mode (read-only)")

  def single_step(self, num_instr=1):
    """
    Single step the core - NOT SUPPORTED in core dump mode
    """
    print("[WARNING] single_step is not supported in core dump mode (read-only)")

  def read_aie_regs(self, reg):
    """
    Reads a register in all of debug aie cores
    """
    if self.use_fallback:
      # Read register from all overlay tiles
      results = []
      for c, r in self.overlay_aie_core_tiles:
        value = self.fallback_reader.read_register(c, r, reg)
        results.append(value)
      return results
    return self.binding.read_regs(reg)

  def write_aie_regs(self, reg, value):
    """
    Write a register in all of debug aie cores - NOT SUPPORTED in core dump mode
    """
    print("[WARNING] write_aie_regs is not supported in core dump mode (read-only)")
