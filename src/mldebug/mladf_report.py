# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2026 Advanced Micro Devices, Inc. All rights reserved.

"""
Help with parsing and mapping for mladf report files
"""

import json
import re

from pathlib import Path

from mldebug.utils import LOGGER


def _compact_id_ranges(ids):
  """Collapse sorted layer ids into comma-separated spans, e.g. ``59,157-209``."""
  if not ids:
    return None
  ids = sorted(set(ids))
  parts = []
  start = prev = ids[0]
  for cur in ids[1:]:
    if cur == prev + 1:
      prev = cur
      continue
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    start = prev = cur
  parts.append(str(start) if start == prev else f"{start}-{prev}")
  return ",".join(parts)


def load_json(path):
  """
  utility
  """
  try:
    with open(path, "r", encoding="utf-8") as f:
      return json.load(f)
  except FileNotFoundError as e:
    print(e)
    return {}


class MladfReport:
  """
  Encapsulates MLADF Details
  """

  def __init__(self, bi_file, m2_file, cps=4):
    """
    bi_file: path to buffer_info.json
    m2_file: path to mladf report
    cps: cols per stamp
    """
    bi_data = load_json(Path(bi_file))
    m2_data = load_json(Path(m2_file))
    self.cps = cps

    self.bi_layers = bi_data.get("layers", {})
    self.m2_layers = m2_data.get("layer_information", {})
    self.bi_to_m2 = self._approach1_map(self.bi_layers, self.m2_layers)
    self._warn_unmapped_compute()

  def get_unclaimed_layers(self):
    """
    mladf layers no buffer_info layer maps to, split into (compute, other).
    """
    # An unclaimed compute layer is a mapping failure; data movement is expected.
    claimed = {k for keys in self.bi_to_m2.values() for k in keys}
    compute, other = [], []
    for key, layer in self.m2_layers.items():
      if key in claimed:
        continue
      (compute if layer.get("is_compute_layer") else other).append(key)
    return compute, other

  def _warn_unmapped_compute(self):
    """Complain when a kernel-running mladf layer maps to no buffer_info layer."""
    compute, _ = self.get_unclaimed_layers()
    if not compute:
      return
    ids = [self.m2_layers[k].get("layer_id") for k in compute]
    ids = sorted(i for i in ids if i is not None)
    LOGGER.log(
      f"[WARNING] {len(compute)} mladf compute layer(s) map to no buffer_info "
      f"layer: {_compact_id_ranges(ids) or '?'}. Their kernels cannot be "
      "stepped or dumped; layer positions may also be off."
    )

  def get_aiec_layers_by_bilo(self, bilo, ids=None):
    """
    aiecompiler layers for a buffer_info layer_order, optionally narrowed to `ids`.
    """
    aiec_layer_keys = self.bi_to_m2.get(bilo, [])
    layers = [self.m2_layers[k] for k in aiec_layer_keys]
    if ids is None:
      return layers
    ids = set(ids)
    return [lyr for lyr in layers if lyr.get("layer_id") in ids]

  def get_running_stamp_count(self, bilo, max_stamps, ids=None):
    """
    Number of per-batch stamps running a kernel for a buffer_info layer, or
    None if the report does not describe all `max_stamps` stamp cores.
    """
    aiec_layers = self.get_aiec_layers_by_bilo(bilo, ids)
    running = []
    described = 0
    for s in range(max_stamps):
      # Batch 0 only: per-batch stamp s is the core at column s*cps, row 0.
      core = f"{s * self.cps}_0"
      infos = [lyr.get("core_information", {}).get(core) for lyr in aiec_layers]
      infos = [i for i in infos if i is not None]
      if not infos:
        continue
      described += 1
      # A core can be listed (its ELF is loaded) with an empty kernel_name,
      # meaning it does not run this layer's compute.
      if any(i.get("kernel_name", "") for i in infos):
        running.append(s)
    # An absent core makes the count untrustworthy; the caller keeps buffer_info's value.
    if described < max_stamps:
      return None
    # Callers treat the count as stamps 0..n-1, so a gap would mislabel them.
    if running != list(range(len(running))):
      LOGGER.log(
        f"[WARNING] Layer {bilo}: mladf running stamps {running} are not a "
        "contiguous prefix; stamp mapping may be wrong."
      )
    return len(running)

  def get_layer_ids_for_bilo(self, bilo):
    """Sorted unique mladf layer_id values mapped to a buffer_info layer."""
    return sorted(
      {
        self.m2_layers[k]["layer_id"]
        for k in self.bi_to_m2.get(bilo, [])
        if "layer_id" in self.m2_layers[k]
      }
    )

  def get_layer_id_segments(self, bilo):
    """
    The layer's mladf `layer_id`s as contiguous runs; >1 run means split scheduling.
    """
    segments = []
    for lid in self.get_layer_ids_for_bilo(bilo):
      if segments and lid == segments[-1][-1] + 1:
        segments[-1].append(lid)
      else:
        segments.append([lid])
    return segments

  def format_layer_id_display(self, ids):
    """
    Format mladf layer_ids for reports: contiguous runs as ``lo-hi``, gaps kept.
    """
    return _compact_id_ranges(ids)

  def get_skname_for_bilo(self, bilo, sid=0, ids=None):
    """
    return superkernel for buffer info layer
    """
    aiec_layers = self.get_aiec_layers_by_bilo(bilo, ids)
    if aiec_layers:
      core = f"{sid * self.cps}_0"
      if aiec_layers[0]["core_information"].get(core):
        try:
          kname = aiec_layers[0]["core_information"][core]["kernel_name"]
          return kname
        except KeyError:
          return ""
      else:
        print(f"[WARNING] MLADF Info for core {core} at Layer_{bilo} not found")
    return ""

  def _get_iters_for_bilo(self, bilo, ids=None):
    """
    find iters for a layer
    """
    aiec_layers = self.get_aiec_layers_by_bilo(bilo, ids)
    if not aiec_layers:
      return 1
    iters = 0
    for aiec_layer in aiec_layers:
      iters += aiec_layer["core_information"]["0_0"]["kernel_repetition"]
    return iters

  def get_elfid_for_bilo(self, bilo, sid, ids=None):
    """
    Find elf ID for buffer info layer order + stamp id
    """
    aiec_layers = self.get_aiec_layers_by_bilo(bilo, ids)
    if not aiec_layers:
      return -1

    core = f"{sid * self.cps}_0"
    pm_info = {}
    if aiec_layers[0]["core_information"].get(core):
      pm_info = aiec_layers[0]["core_information"][core].get("pm_information", {})
    else:
      return -1

    elfs = pm_info.get("elf")
    if not elfs:
      return -1

    if len(elfs) == 1:
      return elfs[0].split("reloadable")[-1]
    for elfid in elfs:
      if "reloadable" in elfid:
        return elfid.split("reloadable")[-1]
    return elfs[0]

  def _extract_m2_parent_graphs(self, kernel_instances_str):
    """
    Extract the set of parent graph names from m2 kernel_node_instances.
    """
    parents = set()
    if not kernel_instances_str:
      return parents

    for inst in kernel_instances_str.split(", "):
      inst = inst.strip()
      if not inst:
        continue

      flexml_match = re.search(r"(flexml_layers\[\d+\])", inst)
      if flexml_match:
        parents.add(flexml_match.group(1))
        continue

      flexml_flat = re.search(r"flexml_layer_(\d+)", inst)
      if flexml_flat:
        parents.add(f"flexml_layers[{flexml_flat.group(1)}]")
        continue

      parts = inst.split(".")
      found = False
      for part in parts:
        if re.search(r"_layer_\d+", part) and "_mk[" not in part:
          parent = re.sub(r"_layer_\d+$", "", part)
          parents.add(parent)
          found = True
          break

      if not found and len(parts) >= 2:
        candidate = re.sub(r"^compute_graph\.", "", inst).split(".")[0]
        if candidate:
          parents.add(candidate)

      # Also add the outermost templated_graph_* part as a candidate parent.
      # Nested kernel instances like
      #   compute_graph.templated_graph__OUTER.templated_graph__OUTER_mha_..._layer_0_0[0].kernel
      # have buffer_info layer_object_name set to the OUTER templated_graph only,
      # so the inner *_layer_N* part picked above never intersects. The trailing
      # strip regex below handles inner names like `..._layer_0_0[0]` too.
      for part in parts:
        if part.startswith("templated_graph_"):
          outer = re.sub(r"_layer_\d+(?:_\d+)*(?:\[\d+\])?$", "", part)
          parents.add(outer)
          break

    return parents

  def _extract_parent_graph(self, name):
    """Extract the parent graph name from a layer_object_name or kernel instance.
    "compute_graph.templated_graph_Generated__0_layer_0"
      -> "templated_graph_Generated__0"
    "compute_graph.flexml_layers[3]"
      -> "flexml_layers[3]"
    """
    stripped = re.sub(r"^compute_graph\.", "", name)
    parent = re.sub(r"_layer_\d+$", "", stripped)
    return parent

  def _kernel_instance(self, m2_layer):
    """The layer's kernel_instance, from the first core that reports one."""
    for core in m2_layer.get("core_information", {}).values():
      kinst = core.get("kernel_instance", "")
      if kinst:
        return kinst
    return ""

  def _approach1_map(self, bi_layers, m2_layers):
    """
    Map each m2 layer to the buffer_info layer that owns it.
    """
    bi_parents = {}
    bi_prefix = {}
    for bi_layer in bi_layers.values():
      bi_key = bi_layer["layer_order"]
      objs = bi_layer.get("layer_object_name", [])
      bi_parents[bi_key] = {self._extract_parent_graph(obj) for obj in objs}
      if bi_layer.get("templated_graph") and len(objs) == 1:
        bi_prefix[bi_key] = f"{objs[0]}."

    m2_parents = {}
    m2_kinst = {}
    for m2_key, m2_layer in m2_layers.items():
      m2_parents[m2_key] = self._extract_m2_parent_graphs(
        m2_layer.get("kernel_node_instances", "")
      )
      m2_kinst[m2_key] = self._kernel_instance(m2_layer)

    bi_to_m2 = {}
    for bi_key, bi_pgraphs in bi_parents.items():
      keys = []
      prefix = bi_prefix.get(bi_key)
      # Exact "<object>." prefix, the key MLProfilerEngine uses. The dot marks the
      # boundary, so templated_graph_10 cannot swallow templated_graph_101_0.
      if prefix:
        keys = [k for k in m2_parents if m2_kinst[k].startswith(prefix)]
      # Looser fallback: regex-derived parent names, so an accidental overlap is
      # possible. Also covers TG layers whose m2 layers report no kernel_instance.
      if not keys:
        keys = [k for k, pgraphs in m2_parents.items() if pgraphs & bi_pgraphs]
      bi_to_m2[bi_key] = keys

    return bi_to_m2
