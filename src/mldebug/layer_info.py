# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Metadata Parsing
"""

import itertools
import json
import os

from dataclasses import dataclass
from pathlib import Path

from mldebug.aie_overlay import Overlay
from mldebug.mladf_report import MladfReport
from mldebug.work_dir import _parse_flexml_layer_id
from mldebug.work_dir import WorkDir
from mldebug.utils import LOGGER
from mldebug.utils import Version

# kernels that don't behave well with breakpoints
unsupported_superkernels = [
  "superkernel_silu1d",
  # Stepping to this causes lock stall
  "mllib_graphs::resize_adf_wrapper",
  # This has many sublayers and needs to be better understood
  "mllib_graphs::mha_type1::mha_adf_wrapper"
  ]


def _strip_template(name):
  """Strip C++ template parameters for compiler-agnostic name comparison.

  Chess _demangle strips templates via .split('<')[0] while MLADF report
  kernel_name and Peano parse_function_sig_llvm preserve them.
  """
  idx = name.find("<")
  return name[:idx] if idx != -1 else name

# For now skip these kernels for end pc
skip_end_pc_kernels = [
  # kernel with 3 end pc release based on depth, width and height iter
  "superkernel_reduction_reduce_mean_c8",
  "superkernel_reduce_mean_c8",
  "superkernel_reducemean_templated",
]

SIZE_BYTES = {
  "bfloat16": 2,
  "float16": 2,
  "int8_t": 1,
  "uint8_t": 1,
  "uint16": 1,  # Size of x2 designs is already in bytes
}


@dataclass
class L1Buffer:
  """
  AIE Core-Tile Buffer
  """

  ping: int
  ping_size: int
  pong: int
  pong_size: int


@dataclass
class L2Buffer:
  """
  AIE Mem Tile/Shared Memory Buffer
  """

  col: int
  row: int
  address: int
  size: int
  buf_id: int
  name: str


class Buffer:
  """
  Top level Buffer e.g: ifm, ofm, wts. Handles mapping and initialization of L1 and L2 buffers.
  """

  def __init__(self, entry, buf_type, size_shift, aie_iface, ifm=False, ofm=False, wts=False):
    """
    Initializes a Buffer object that stores L1 and L2 buffer mappings.

    Args:
        entry (dict): Buffer metadata dictionary.
        buf_type (str): Buffer type: ifm, ofm, or wts.
        size_shift (int): Size shift parameter.
        aie_iface: AIE interface object (provides MEM_TILE_SZ).
        ifm (bool, optional): Is input feature map buffer.
        ofm (bool, optional): Is output feature map buffer.
        wts (bool, optional): Is weights buffer.
    """
    self.l1 = None
    self.l2 = []
    self.type = buf_type
    self.ifm = ifm
    self.ofm = ofm
    self.wts = wts
    size_shift = self._get_size_shift(entry, size_shift)

    if "l1_ping" in entry:
      ping = entry["l1_ping"]
      pong = entry["l1_pong"]
      self.l1 = L1Buffer(int(ping[0], 16), ping[1] * size_shift, int(pong[0], 16), pong[1] * size_shift)
    
    # Handle both "l2" format and "l2_ping/l2_pong" format
    l2_bufs_list = []
    if "l2_ping" in entry:
      # Ping-pong format: collect both ping and pong buffers
      l2_bufs_list.append((entry.get("l2_ping", []), entry.get("l2_ping_buffer_names", [])))
      if "l2_pong" in entry:
        l2_bufs_list.append((entry.get("l2_pong", []), entry.get("l2_pong_buffer_names", [])))
    else:
      # Standard format
      l2_bufs_list.append((entry.get("l2", []), entry.get("l2_buffer_names", [])))

    buf_id = 0
    # Process all L2 buffer sets (ping, pong, or standard)
    for l2_bufs, l2_names in l2_bufs_list:
      for buf, buf_name in itertools.zip_longest(l2_bufs, l2_names, fillvalue=None):
        if not buf or len(buf) < 4:
          break
        if not buf_name:
          buf_name = ""
        c = buf[0]
        # buffer_info MEM tiles are at row 0
        r = buf[1] + 1
        addr = buf[2]
        size = buf[3] * size_shift
        # split_msg = f"L2 {c},{r} {addr}-{size}: "
        # Check if buffer spills into next n cols
        while size - (aie_iface.MEM_TILE_SZ - addr) > 0:
          # split_msg += f" {addr}-{aie_iface.MEM_TILE_SZ - addr}"
          self.l2.append(L2Buffer(c, r, addr, aie_iface.MEM_TILE_SZ - addr, buf_id, buf_name))
          size -= aie_iface.MEM_TILE_SZ - addr
          addr = 0
          c += 1
        # split_msg += f" {addr}-{size}"
        self.l2.append(L2Buffer(c, r, addr, size, buf_id, buf_name))
        # LOGGER.verbose_print(split_msg)
        buf_id += 1

  def _get_size_shift(self, entry, size_shift):
    """
    Get the size in bytes for the specific buffer dtype, falling back to size_shift.

    Args:
        entry (dict): Buffer metadata dictionary with optional 'dtype'.
        size_shift (int): Fallback size shift if dtype absent.

    Returns:
        int: Calculated byte shift for buffer data.
    """
    # dtype is a per-buffer param
    dtype = entry.get("dtype")
    # int8 and uint8 are 1 byte
    if dtype is not None:
      return SIZE_BYTES.get(dtype, 1)
    # size shift is a global param
    # size_shift 0 => int8
    # size_shift 1 => bf16
    if size_shift is not None:
      return size_shift + 1
    return 1


@dataclass
class Lcp:
  """
  Layer control parameters for iteration, depth, and buffer handling.
  """

  num_iter: int = 1
  depth_iter: int = 1
  buffer_iter: int = 1
  super_iter: int = 1
  wts_iter: int = 1
  is_tg: bool = False


@dataclass
class Stamp:
  """
  Stores the stamp (superkernel) metadata for an AIE kernel.

  Attributes:
      name (str): Kernel name.
      start_pc (int): Start program counter.
      end_pc (int): End program counter.
      elf_name (str): Associated ELF object.
  """
  name: str
  start_pc: int = 0
  end_pc: int = 0
  elf_name: str = ""


@dataclass
class L3Buffer:
  """
  Stores the metadata for L3 (DDR) buffer as described in buffer_info.

  Attributes:
      name (str): Buffer name.
      tensor_name (str): Tensor name.
      offset (int): Offset in parent buffer.
      size (int): Buffer size in bytes.
  """

  name: str
  tensor_name: str
  offset: int
  size: int


class Layer:
  """
  Represents an abstract Layer which could be at BE/AIECompiler Level.
  Contains all buffer, iteration, and kernel (stamp) mapping information.
  """

  def __init__(self, info, size_shift, version, aie_iface, num_stamps, mladf_report):
    """
    Initialize a Layer object using given metadata, populating buffer and kernel/stamp lists.

    Args:
        info (dict): Layer metadata.
        size_shift (int): Size shift parameter.
        version: Software version object.
        aie_iface: AIE interface object.
        num_stamps (int): Number of stamps in overlay.
    """
    self.flexml_ids = []
    self.l3_ifm_buffers = []
    self.l3_ofm_buffers = []
    self.l3_buffers = []
    self.in_buffers = []
    self.out_buffers = []
    self.wts_buffers = []
    self.layer_order = info["layer_order"]
    self.lcp = Lcp()
    self.pm_work_dir = info.get("pm", None)
    self.is_unsupported = False
    self.is_concat = False

    self.lcp.is_tg = "templated_graph" in info
    kname = [i.lower() for i in info["kernel_name"]][0]

    if self.lcp.is_tg and not mladf_report:
      self.is_unsupported = True
      return

    n_stamps = info.get("no_of_stamps")
    if n_stamps and n_stamps < num_stamps:
      num_stamps = n_stamps
    self.stamps = [Stamp(name=kname) for _ in range(num_stamps)]

    if self.lcp.is_tg:
      for sid, stamp in enumerate(self.stamps):
        stamp.name = mladf_report.get_skname_for_bilo(self.layer_order, sid)
        stamp.elf_name = mladf_report.get_elfid_for_bilo(self.layer_order, sid)
        if not stamp.name or stamp.elf_name == -1 or any(k in stamp.name for k in unsupported_superkernels):
          LOGGER.verbose_print(f"[WARNING] unsupported kernel {stamp.name} at Layer {self.layer_order} will be skipped.")
          self.is_unsupported = True
          return
      self.lcp.num_iter = mladf_report._get_iters_for_bilo(self.layer_order)

    self._initialize_l3_buffers(info, version)
    # 1. Layers without any kernel should be skipped
    # 2. Unsupported superkernel should be skipped
    self.is_concat = info.get("is_concat") or not kname
    if self.is_concat:
      LOGGER.verbose_print(f"[WARNING] unsupported kernel {kname} at Layer {self.layer_order} will be skipped.")
      self.is_unsupported = True
      return

    if self.lcp.is_tg:
      return

    self._initialize_flexml_ids(info)
    self._initialize_buffers(info, aie_iface, size_shift, version)
    self._initialize_iters(info, version)
    LOGGER.verbose_print(f"{self.layer_order}: {kname} {self.lcp.num_iter}")

  def _initialize_flexml_ids(self, info):
    """
    Populate self.flexml_ids for this layer from its metadata.

    Args:
        info (dict): Layer metadata.
    Notes:
        - Handles new ("no_of_stamps") and old formats.
    """
    if "pm" in info:
      self.flexml_ids.append(info["layer_order"])
      return
    if "no_of_stamps" in info:
      # New format
      for objname in info.get("layer_object_name", []):
        flid = _parse_flexml_layer_id(objname)
        if flid != -1:
          self.flexml_ids.append(flid)
    else:
      # Old format
      objname = info.get("layer_object_name", "")
      flid = _parse_flexml_layer_id(objname)
      if flid != -1:
        self.flexml_ids.append(_parse_flexml_layer_id(objname))

  def _initialize_iters(self, info, version):
    """
    Populate Lcp (Layer control parameter) iteration counts from metadata.

    Args:
        info (dict): Layer metadata.
        version (Version): Parsed flexml version.
    Notes:
        - Maps number of iterations, depth, buffer and weights transfer intervals.
    """
    self.lcp.num_iter = info.get("num_iter", 1)
    if self.lcp.num_iter == 0:
      self.lcp.num_iter = 1
    # Depth Iter: Interval at which L1->L2 OFM Transfer occurs
    di_key = "depth_iter"
    if version == Version.from_string("1.0"):
      di_key = "l1_depth_iter"
    self.lcp.depth_iter = info.get(di_key, 1)
    # Buffer Iter: Number of L3->L2 IFM transfers for a layer
    # Calculate interval. Default: single transfer
    self.lcp.buffer_iter = int(self.lcp.num_iter / info.get("buffer_iter", 1))
    # Super Iter: Number of L2->L3 OFM spills for a layer
    # Calculate interval. Default: depth iter interval
    self.lcp.super_iter = self.lcp.depth_iter
    si = info.get("super_iter", self.lcp.num_iter)
    if si:
      self.super_iter = int(self.lcp.num_iter / si)
    # Wts Iter: Number of L3->L2 WTS transfers for a layer
    # Calculate interval. Default: single transfer
    self.wts_iter = int(self.lcp.num_iter / info.get("wts_iter", 1))

  def _match_l3_buffer(self, fm, l3_buffer_names, l3_buffer_sizes, size_shift):
    """
    Helper function to match the L3 buffer sizes to full buffer names.

    Args:
        fm (str): Feature map type ("ifm", "ofm", ...).
        l3_buffer_names (list or str): List or single name of L3 buffer(s).
        l3_buffer_sizes (dict): Mapping of sub-names to size/type information.
        size_shift (int): Size shift to apply on buffer size.

    Raises:
        RuntimeError: If a sub-buffer is not found among known L3 buffer names.
    Notes:
        - Handles varying buffer naming/typing.
    """
    # TODO remove this check when buffer_info always prints l3_names as list
    if isinstance(l3_buffer_names, str):
      l3_buffer_names = [l3_buffer_names]
    for sub_name, size in l3_buffer_sizes.items():
      is_substr = False
      for full_l3_name in l3_buffer_names:
        if sub_name in full_l3_name:
          if size.get("type"):
            size_shift = SIZE_BYTES.get(size["type"], 1)
          buffer = L3Buffer(
            name=full_l3_name, tensor_name=size.get("tensor_name"), size=int(size["size"] * size_shift), offset=None
          )
          if "ifm" in fm:
            self.l3_ifm_buffers.append(buffer)
          else:
            self.l3_ofm_buffers.append(buffer)
          is_substr = True
          break

      if not is_substr:
        raise RuntimeError(f"The sub-name {sub_name} is not in the list of full L3 names {l3_buffer_names}")

  def _initialize_l3_buffers(self, info, version):
    """
    Initializes ifm/ofm L3 buffers for this layer from metadata.

    Args:
        info (dict): Layer metadata.
        version (Version): Parsed flexml version.
    """
    # Many entries have the string "ifm" in them
    if version > Version.from_string("1.2"):
      if "ifm" in info:
        for _, entry in enumerate(info["ifm"], start=1):
          if "l3" in entry:
            self._match_l3_buffer("ifm", entry["l3_buffer_names"], entry["l3"], SIZE_BYTES.get(entry.get("dtype"), 1))
    else:
      fms = ["ifm", "ifm2", "ofm"]
      for name in fms:
        if name in info and "l3" in info[name]:
          self._match_l3_buffer(
            name, info[name]["l3_buffer_names"], info[name]["l3"], SIZE_BYTES.get(info[name].get("dtype"), 1)
          )

  def _initialize_buffers(self, info, aie_iface, size_shift, version):
    """
    Initializes the L1/L2 buffers for each layer (input, output, weights).

    Args:
        info (dict): Layer metadata.
        aie_iface: AIE interface object.
        size_shift (int): Size shift parameter from .meta.
        version (Version): Software version object.
    """
    # Many entries have the string "ifm" in them
    if version > Version.from_string("1.2"):
      if "ifm" in info:
        for i, entry in enumerate(info["ifm"], start=1):
          self.in_buffers.append(Buffer(entry, f"ifm{i}", size_shift, aie_iface, ifm=True))
    else:
      for name in ["ifm", "ifm2"]:
        if name in info:
          self.in_buffers.append(Buffer(info[name], name, size_shift, aie_iface, ifm=True))
    for name in ["wts"]:
      if "wts" in info:
        self.wts_buffers.append(Buffer(info[name], name, size_shift, aie_iface, wts=True))
    for name in ["ofm"]:
      if name in info:
        self.out_buffers.append(Buffer(info[name], name, size_shift, aie_iface, ofm=True))

  def __str__(self):
    """
    Pretty-print metainfo about this layer. Returns kernel and iteration info.
    """
    s = f"iters: {self.lcp.num_iter}"
    for i, stamp in enumerate(self.stamps):
      s += f"\n  Stamp {i}: elf: {stamp.elf_name}, kernels: {stamp.name}"
      s += f", start pc: {stamp.start_pc}, final lock release pc: {stamp.end_pc}"
    return s


class LayerInfo:
  """
  Container for metadata and analysis of all BE layers in an AIE design.
  Handles setup, parsing, mapping, and access to layer/kernels/buffers.
  """

  def __init__(self, args):
    """
    Main entry for initializing all layer info from CLI arguments or parsed environment.

    Args:
        args: Namespace of configuration and input files (from argparser or similar).
    """
    self.layers = []
    self.layout = [1, 4, 4]
    self.aie_iface = args.aie_iface
    self.x2 = False
    self.x2_work_dirs = {}
    self.layer_workdir_map = {}
    self.device_batch_size = 1
    self.mladf_report = None

    has_bi = args.buffer_info and Path(args.buffer_info).is_file()
    use_mladf = args.mladf_report and Path(args.mladf_report).is_file() and args.run_flags.enable_tg
    data = None
    # 1. Parse the buffer info to get Layout
    if has_bi:
      data = self._read_buffer_info(args.buffer_info)
    # 2. Initialize Overlay from Layout
    self.overlay = Overlay(args, self.layout)
    # 3. Parse mladf report.
    # TBD: memory optimize this as this json can be large
    if not args.aie_only and has_bi and use_mladf:
      self.mladf_report = MladfReport(args.buffer_info, args.mladf_report, self.overlay.get_stampwidth())
    # 4. Initialize Layers
    if not args.aie_only:
      num_stamps = len(self.overlay.get_stampids())
      self._init_layers(data, args.aie_iface, num_stamps)
    # 5: Parse work dir
    if self.x2:
      for layer in self.layers:
        if layer.pm_work_dir:
          path = os.path.join(args.aie_dir, layer.pm_work_dir)
          if layer.pm_work_dir not in self.x2_work_dirs:
            self.x2_work_dirs[layer.pm_work_dir] = WorkDir(path, args.peano, self.overlay)
          self.layer_workdir_map[layer.layer_order] = self.x2_work_dirs[layer.pm_work_dir]
      self.work_dir = next(iter(self.layer_workdir_map.values()))
    else:
      self.work_dir = WorkDir(args.aie_dir, args.peano, self.overlay, args.run_flags.dump_temps)

    if not args.aie_only:
      # Set PC Value for layers
      if self.x2:
        self._initialize_layers_from_workdir_x2(args)
      else:
        self._initialize_layers_from_workdir(args)

  def update_work_dir(self, layer_order):
    """
    Updates the work directory reference for X2 designs for the given layer order.

    Args:
        layer_order (int): Layer order used as key.
    """
    if self.x2:
      self.work_dir = self.layer_workdir_map[layer_order]

  def print_aie_functions(self, elf_id=None):
    """
    Print the AIE functions for all or selected ELFs for debugging support.

    Args:
        elf_id (optional): If specified, only prints info for this ELF.
    """
    if self.x2:
      sep = "--------------------------------------------"
      for name, work_dir in self.x2_work_dirs.items():
        print(f"{sep}\nWork dir: {name}")
        work_dir.print_aie_functions(elf_id)
    else:
      self.work_dir.print_aie_functions(elf_id)
    print("[INFO] It is safer to break at lock release PC as compared to end pc")

  def is_stamped(self):
    """
    Check if design is a multi-stamp (multi-superkernel) program.

    Returns:
        bool: True if stamped/multi-stamp, False otherwise.
    """
    return len(self.overlay.get_stampids()) > 1

  def is_batched(self):
    """
    Check if design is batched (runs >1 samples at a time).

    Returns:
        bool: True if more than one batch, False otherwise.
    """
    return self.device_batch_size > 1

  def _create_info(self):
    """
    Build a mapping of {stamp_id: {elf: [min_layer, max_layer], ...}} across all layers.

    Returns:
        dict: Mapping suitable for overview/human printout.
    """
    info = {}
    for n in range(len(self.overlay.get_stampids())):
      info[n] = {}
    for layer in self.layers:
      order = layer.layer_order
      for sid, stamp in enumerate(layer.stamps):
        imap = info[sid]
        elf = stamp.elf_name
        if not elf:
          continue
        if elf not in imap:
          imap[elf] = [order, order]
        else:
          imap[elf][0] = min(imap[elf][0], order)
          imap[elf][1] = max(imap[elf][1], order)
    return info

  def print_info(self):
    """
    Print an overview of the loaded/stamped layers in human readable format.
    """
    info = self._create_info()
    sep = "--------------------------------------------"
    m = "Design info (Excluding TG Layer IDs)\n"
    m += f"{sep}\nFlexml Layer Count: {len(self.layers)}\n{sep}"
    if not self.work_dir.elf_flxmlid_maps or not self.layers:
      return
    for sid, imap in info.items():
      m += f"\nStamp {sid}: "
      for eid, (min_layer, max_layer) in imap.items():
        if min_layer == max_layer:
          m += f"{{{eid}: {min_layer}}} "
        else:
          m += f"{{{eid}: {min_layer}-{max_layer}}} "
    m += "\n"
    m += "--------------------------------------------"
    LOGGER.log(m)

  def initialize_l3_offsets(self, flexmlrt_hsi, external_buffer_id):
    """
    Initialize and return a dict of {L3 buffer name: offset} for all Layer L3 buffers, reading metadata files.

    Args:
        flexmlrt_hsi (str): Path to flexmlrt-hsi JSON metadata.
        external_buffer_id (str): Path to external_buffer_id JSON (X2 mainly).

    Returns:
        dict: Mapping buffer name to its byte offset in parent buffer.
    """
    l3_offsets = {}
    if self.x2:
      with open(external_buffer_id, "r", encoding="utf-8") as file:
        data = json.load(file)

      data = data["external_buffers"]
      # For now, assume only a single parent spill buffer - TODO: handle multiple DDR L3 buffers
      # external_buffer_id locations are already in bytes
      for buffer in data:
        if "name" in data[buffer] and data[buffer]["name"] == "coalesed_spills":
          if "coalesed_buffers" in data[buffer]:
            for spill_buffer in data[buffer]["coalesed_buffers"]:
              if "name" in spill_buffer and "offset_in_bytes" in spill_buffer:
                l3_offsets[spill_buffer["name"]] = spill_buffer["offset_in_bytes"]
    else:
      with open(flexmlrt_hsi, "r", encoding="utf-8") as file:
        data = json.load(file)

      # For now, assume only a single parent spill buffer - TODO: handle multiple DDR L3 buffers
      if "spills" not in data:
        LOGGER.log("WARNING: No L3 Spill Buffers found in flexmlrt-hsi.json")
        for layer in self.layers:
          layer.l3_buffers = []
        return {}

      spill_data = data["spills"]["layers"]
      for layer in spill_data:
        if "name" in layer and "offset" in layer:
          l3_offsets[layer["name"]] = 4 * layer["offset"]
    return l3_offsets

  def initialize_l3_layer_mapping(self, flexmlrt_hsi, external_buffer_id):
    """
    Update all layers with their proper L3 buffer lists (and offsets)
    based on mapping from flexmlrt JSON and device mode (x2 or not).

    Args:
        flexmlrt_hsi (str): Path to flexmlrt-hsi.json.
        external_buffer_id (str): Path to external_buffer_id JSON.

    Side Effects:
        - Modifies each layer's l3_buffers field as appropriate.
        - Handles multi-stamp (batched), X2, TG buffers, and overlap conflicts.
    """
    l3_offsets = self.initialize_l3_offsets(flexmlrt_hsi, external_buffer_id)

    for layer in self.layers:
      layer.l3_buffers = layer.l3_ofm_buffers if self.x2 else layer.l3_ifm_buffers

      # Duplicate L3 buffers for multi-stamp designs (batched designs)
      if self.is_batched():
        original_buffers = list(layer.l3_buffers)
        for stamp_idx in range(1, self.device_batch_size):
          for orig_buffer in original_buffers:
            stamped_buffer = L3Buffer(
              name=f"{orig_buffer.name}_stamp_{stamp_idx}",
              tensor_name=orig_buffer.tensor_name,
              size=orig_buffer.size,
              offset=None
            )
            layer.l3_buffers.append(stamped_buffer)

      for idx, l3_buffer in enumerate(layer.l3_buffers):
        if l3_buffer.name in l3_offsets:
          layer.l3_buffers[idx].offset = l3_offsets[l3_buffer.name]
        else:
          # Could be an ifm/ofm buffer. For now remove it
          LOGGER.verbose_print(
            f"WARNING: L3 Buffer {l3_buffer.name} not found in spill data. Removing it from layer {layer.layer_order}"
          )

      # Remove missing buffers
      layer.l3_buffers = [buf for buf in layer.l3_buffers if buf.offset is not None]

    # X2 uses OFM buffers so dumping should occur at the next layer
    if self.x2:
      prev_buffers = []
      for layer in self.layers:
        current_buffers = layer.l3_buffers
        layer.l3_buffers = prev_buffers
        prev_buffers = current_buffers

  def _check_l3_tg_conflicts(self, l3_buffers):
    """
    Check for conflicts in L3 buffers for TG layers. With the L3 buffer re-use
    optimizations, consecutive TG L3 buffers may be overwritten. If there was
    a conflict, we can only use the last buffer that wrote to that overlap.

    Args:
        l3_buffers (list[L3Buffer]): List of L3 buffers to check.

    Returns:
        list[L3Buffer]: Pruned list, removing conflicted/overlapped buffers.
    """
    # Go in order and check each conflicting pair. If there's an overlap, we can remove the current buffer
    updated_l3_buffers = []

    for i, current_buffer in enumerate(l3_buffers):
      has_overlap = False

      for j in range(i + 1, len(l3_buffers)):
        compared_buffer = l3_buffers[j]
        if self._overlap(current_buffer, compared_buffer):
          LOGGER.verbose_print(
            f"WARNING: L3 Buffer {current_buffer} overlaps with {compared_buffer}. Removing {current_buffer}"
          )
          has_overlap = True
          break

      if not has_overlap:
        updated_l3_buffers.append(current_buffer)

    return updated_l3_buffers

  def _overlap(self, buf1, buf2):
    """
    Returns true if there is a byte-range overlap between two buffer objects.

    Args:
        buf1, buf2 (L3Buffer): Buffers to check overlap.

    Returns:
        bool: True if overlap detected, False otherwise.
    """
    start1 = buf1.offset
    end1 = start1 + buf1.size

    start2 = buf2.offset
    end2 = start2 + buf2.size
    return end1 > start2 and end2 > start1

  def _read_buffer_info(self, buffer_info_file):
    """
    Load and parse the buffer_info JSON, extracting layout and batch size.

    Args:
        buffer_info_file (str): Path to buffer_info JSON.

    Returns:
        dict: Parsed JSON object from file.
    Side Effects:
        - Sets self.layout, self.device_batch_size, self.x2.
    """
    print("Initializing Buffer Info ...")
    with open(buffer_info_file, encoding="utf-8") as fd:
      data = json.load(fd)
    self.layout = data[".meta"].get("layout")
    self.device_batch_size = data[".meta"].get("device_batch_size", 1)

    # Treat mBnS as 1BnS
    if self.device_batch_size > 1:
      if self.layout[0] > 1:
        LOGGER.log("[WARNING] Currently mBatch x nStamp is unsupported. Setting batchcount to 1.")
        self.device_batch_size = 1
      else:
        self.layout[0] = self.device_batch_size
        LOGGER.log("Batched design detected")

    for _, e in data.items():
      if "no_of_stamps" in e:
        self.layout[0] = max(self.layout[0], e.get("no_of_stamps"))
    self.x2 = data[".meta"].get("flow") == "x2"
    return data

  def _init_layers(self, raw_info, aie_iface, num_stamps):
    """
    Parse all layer entries from metadata and populate self.layers.

    Args:
        raw_info (dict): Parsed buffer_info JSON metadata.
        aie_iface: AIE interface object.
        num_stamps (int): Number of stamps identified from overlay.
    """
    version = Version.from_string(raw_info[".meta"]["version"])
    size_shift = raw_info[".meta"].get("size_shift")
    # New style
    if "layers" in raw_info:
      raw_layers = raw_info["layers"]
    # Old style
    else:
      del raw_info[".meta"]
      if "buffer_map" in raw_info:
        del raw_info["buffer_map"]
      raw_layers = raw_info
    # Create layers
    raw_layers = sorted(raw_layers.items(), key=lambda item: item[1]["layer_order"])
    for entry in raw_layers:
      info = entry[1]
      layer = Layer(info, size_shift, version, aie_iface, num_stamps, self.mladf_report)
      self.layers.append(layer)

  def _initialize_layers_from_workdir_x2(self, args):
    """
    Initialize layers specifically for x2 work directories.

    Args:
        args: Top level argument object
    Raises:
        RuntimeError: If no non-TG layers remain after trim.
    Side Effects:
        Updates start/end PC for each layer stamp when found.
    """
    flexmlrt_hsi = args.flexmlrt_hsi
    external_buffer_id = args.external_buffer_id

    self.initialize_l3_layer_mapping(flexmlrt_hsi, external_buffer_id)
    # Trim dma_only layers
    self.layers = [layer for layer in self.layers if not layer.lcp.is_tg]
    if not self.layers:
      raise RuntimeError("No layers found in the design.")
    for sid in self.overlay.get_stampids():
      for layer in self.layers:
        flist = list(self.layer_workdir_map[layer.layer_order].aie_functions[sid].values())[0]
        self.layer_workdir_map[layer.layer_order].pm_reload_en[sid] = True
        for f in flist:
          if _strip_template(layer.stamps[sid].name.lower()) == _strip_template(f.name.lower()):
            stamp = layer.stamps[sid]
            LOGGER.verbose_print("Layer found:", layer.layer_order, stamp.name, f.start_pc)
            stamp.elf_name = layer.pm_work_dir
            stamp.start_pc = f.start_pc
            if f.name.lower() not in skip_end_pc_kernels:
              stamp.end_pc = f.final_lock_release_pc

  def _initialize_layers_from_workdir(self, args):
    """
    Go through the detected metadata and initialize the layers.

    Args:
        args: Top level argument object

    Raises:
        RuntimeError: If no supported layers are found after trim.
    Side Effects:
        Updates each layer's stamp with elf, start, and end PCs, as matched in AIE work dir info.
    """
    flexmlrt_hsi = args.flexmlrt_hsi
    external_buffer_id = args.external_buffer_id

    # Initialize l3 buffers before trimming TG layers
    if flexmlrt_hsi and os.path.exists(flexmlrt_hsi):
      self.initialize_l3_layer_mapping(flexmlrt_hsi, external_buffer_id)

    # Trim unsupported layers
    self.layers = [layer for layer in self.layers if not layer.is_unsupported]
    # Designs with no real layers
    if not self.layers:
      return

    # Hierarchy of Data:
    # Stamp <- Elf <- Layers
    # AIECompiler only knows flexmlIDs so we use that to match with correct layer
    for sid in self.overlay.get_stampids():
      has_pm_reload = self.work_dir.pm_reload_en[sid]
      for elf_name, flist in self.work_dir.aie_functions[sid].items():
        LOGGER.verbose_print(f"Initializing layers for stamp {sid} ELF: {elf_name}")
        elf_id = elf_name.split("reloadable")[-1]
        for f, l in itertools.product(flist, self.layers):
          if sid > len(l.stamps) - 1:
            continue
          if _strip_template(l.stamps[sid].name.lower()) == _strip_template(f.name.lower()):
            stamp = l.stamps[sid]
            if l.lcp.is_tg and stamp.elf_name == elf_id:
              stamp.start_pc = f.start_pc
              if f.name.lower() not in skip_end_pc_kernels:
                stamp.end_pc = f.final_lock_release_pc
              continue
            # Check if this layer is present in the elf
            # In buffer_info the flexml_ids might not be in order of stamps
            if has_pm_reload and not any(i in self.work_dir.elf_flxmlid_maps[sid][elf_id] for i in l.flexml_ids):
              continue
            LOGGER.verbose_print("Layer found:", l.layer_order, stamp.name)
            stamp.elf_name = elf_id
            stamp.start_pc = f.start_pc
            if f.name.lower() not in skip_end_pc_kernels:
              stamp.end_pc = f.final_lock_release_pc

    # Under right conditions, we don't even go through iterations
    if args.run_flags.skip_iter and args.run_flags.enable_tg:
      for idx, layer in enumerate(self.layers):
        if idx >= len(self.layers) - 1:
          layer.lcp.num_iter = 1
          break
        next_layer_stamps = self.layers[idx+1].stamps
        if (layer.stamps[0].name != next_layer_stamps[0].name
          and len(layer.stamps) == len(next_layer_stamps)
          and all(layer.stamps[i].elf_name == next_layer_stamps[i].elf_name for i in range(len(layer.stamps)))
          ):
          layer.lcp.num_iter = 1
