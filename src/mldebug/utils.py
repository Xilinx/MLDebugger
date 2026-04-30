# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Helpful utilities like logging
"""

from pathlib import Path

import os
import platform
import time


class Logger:
  """
  Manage logging and printing for the debugger.
  """

  def __init__(self):
    """
    Initialize a Logger instance.
    """
    self.run_log_path = "debug_logs"
    self.verbose = False
    self.log_handle = None
    self.flush_disabled = False

  def setup(self, args):
    """
    Configure the logger at runtime.

    Args:
      args (object): Argument namespace or object with attributes:
        - verbose (bool): Enable verbose printing if True.
        - flush_disabled (bool): If True, disables flush (no \r output).
        - name (str): Optional, used for log file naming.
    """
    self.verbose = args.verbose
    self.flush_disabled = args.flush_disabled
    log_file_name = "mldebug.log"
    if args.name:
      Path(self.run_log_path).mkdir(exist_ok=True)
      log_file_name = f"./{self.run_log_path}/log_{args.name}.txt"
    self.log_handle = None
    if args.verbose:
      self.log_handle = open(log_file_name, "w", encoding="utf-8")

  def log(self, msg, display=True, flush=False, log=True):
    """
    Print/log a message to file or stdout, with handle for carriage return/flush.

    Args:
      msg (str): Message to log/print.
      display (bool, optional): If True, print to stdout. Default: True.
      flush (bool, optional): If True, flush stdout and print with '\r'. Default: False.
      log (bool, optional): If True, write to log file if enabled. Default: True.
    """
    if self.flush_disabled:
      flush = False
    if self.log_handle and log:
      self.log_handle.write(msg + "\n")
    if display:
      # Carriage return enables output update in one line
      end = "\n" if not flush else "\r"
      print(msg, end=end, flush=flush)

  def verbose_print(self, *args, **kwargs):
    """
    Print arguments to stdout only if verbosity is enabled.

    Args:
      *args: Arguments to print (passed to built-in print).
      **kwargs: Keyword arguments for print.
    """
    if self.verbose:
      print(*args, **kwargs)

  def close(self):
    """
    Close the log file handle if opened.
    """
    if self.log_handle:
      self.log_handle.close()


LOGGER = Logger()


def setup_logger(args):
  """
  Initialize the global Logger with the given arguments.

  Args:
    args (object): Argument namespace or object for Logger.setup().
  """
  LOGGER.setup(args)


def close_logger():
  """
  Finalize and close the global Logger.
  """
  LOGGER.close()


class Version:
  """
  Simple versioning class for (major, minor) version management.
  """

  def __init__(self, major, minor):
    """
    Initialize a Version instance.

    Args:
      major (int or str): Major version number.
      minor (int or str): Minor version number.
    """
    self.major = int(major)
    self.minor = int(minor)

  def __str__(self):
    """
    Return version as "major.minor" string.
    """
    return f"{self.major}.{self.minor}"

  def __repr__(self):
    """
    Return official string representation.
    """
    return f"Version({self.major}, {self.minor})"

  def __eq__(self, other):
    """
    Check equality between two Version objects.

    Args:
      other (Version): The other Version to compare.

    Returns:
      bool: True if major and minor equal, else False.
    """
    if isinstance(other, Version):
      return (self.major, self.minor) == (other.major, other.minor)
    return False

  def __lt__(self, other):
    """
    Compare two Version objects (less than).

    Args:
      other (Version): The other Version to compare.

    Returns:
      bool: True if self is less than other.

    Raises:
      TypeError: If compared to a non-Version object.
    """
    if isinstance(other, Version):
      return (self.major, self.minor) < (other.major, other.minor)
    raise TypeError("Invalid comparison between Version and non-Version type")

  def __gt__(self, other):
    """
    Compare two Version objects (greater than).

    Args:
      other (Version): The other Version to compare.

    Returns:
      bool: True if self is greater than other.

    Raises:
      TypeError: If compared to a non-Version object.
    """
    if isinstance(other, Version):
      return (self.major, self.minor) > (other.major, other.minor)
    raise TypeError("Invalid comparison between Version and non-Version type")

  def __le__(self, other):
    """
    Compare two Version objects (less than or equal).

    Args:
      other (Version): The other Version to compare.

    Returns:
      bool: True if self is less than or equal to other.
    """
    return self < other or self == other

  def __ge__(self, other):
    """
    Compare two Version objects (greater than or equal).

    Args:
      other (Version): The other Version to compare.

    Returns:
      bool: True if self is greater than or equal to other.
    """
    return self > other or self == other

  @classmethod
  def from_string(cls, version_str):
    """
    Create Version object from string, e.g. "1.2".

    Args:
      version_str (str): Version string in "major.minor" format.

    Returns:
      Version: Instance representing parsed version.

    Raises:
      ValueError: If the string is not in the correct format.
    """
    try:
      major, minor = version_str.split(".")
      return cls(major, minor)
    except ValueError as e:
      raise ValueError(f"Invalid version format: {version_str}") from e


def timeit(func):
  """
  Decorator to time function execution and print duration.

  Args:
    func (callable): Function to time.

  Returns:
    callable: Wrapped function with timing print.
  """

  def wrapper(*args, **kwargs):
    start = time.time()
    result = func(*args, **kwargs)
    end = time.time()
    print(f"'{func.__name__}' took {end - start:.2f}s")
    return result

  return wrapper


def print_tile_grid(title, tiles, register_values=None, format_type="hex"):
  """
  Prints a grid visualization of tile information and optional register values.

  Args:
    title (str): The grid title to display.
    tiles (list[tuple[int, int]]): List of (col, row) tile tuples to show.
    register_values (list, optional): List of register/status values parallel to tiles, or None.
    format_type (str, optional): If "hex", show values as hex strings (default). If "int", show as decimal.

  Returns:
    None
  """
  if not tiles:
    print(f"\n===== {title} =====")
    print("No tiles available")
    print("==============================\n")
    return

  min_col = min(c for c, r in tiles)
  max_col = max(c for c, r in tiles)
  min_row = min(r for c, r in tiles)
  max_row = max(r for c, r in tiles)

  register_dict = {}
  if register_values is None:
    register_dict = {(c, r): "0xdead" for c, r in tiles}
  else:
    # Use provided status values
    for idx, (c, r) in enumerate(tiles):
      if isinstance(register_values[idx], int):
        if format_type.lower() == "hex":
          register_dict[(c, r)] = f"0x{register_values[idx]:04x}"
        else:  # int format
          register_dict[(c, r)] = f"{register_values[idx]:4d}"
      else:
        register_dict[(c, r)] = str(register_values[idx])

  cell_width = 9
  row_label_width = 4
  total_width = row_label_width + cell_width * (max_col - min_col + 1)

  equals_count = (total_width - len(title) - 2) // 2
  print(f"\n{'=' * equals_count} {title} {'=' * equals_count}")

  for row in range(max_row, min_row - 1, -1):
    row_str = f"{row:2d} |"
    for col in range(min_col, max_col + 1):
      if (col, row) in register_dict:
        row_str += f" {register_dict[(col, row)]:8s}"
      else:
        row_str += " " * 9
    print(row_str)

  col_header = "   "
  for col in range(min_col, max_col + 1):
    col_header += f"    {col:2d}   "
  print(col_header)

  print(f"{'=' * total_width}")

def is_aarch64():
  """
  ARM
  """
  return platform.machine().lower() in ['aarch64', 'arm64']

def is_windows():
  """
  x86 Windows
  """
  return os.name == "nt"

def is_linux():
  """
  x86 Linux
  """
  return platform.system() == "Linux" and not is_aarch64()
