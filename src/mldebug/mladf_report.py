# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2026 Advanced Micro Devices, Inc. All rights reserved.

"""
Help with parsing and mapping for mladf report files
"""

import json
import re

from pathlib import Path

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

  def get_aiec_layers_by_bilo(self, bilo):
    """
    Return list of aiecompiler layers for a specific
    layer_order in buffer_info
    """
    aiec_layer_keys = self.bi_to_m2.get(bilo, [])
    return [self.m2_layers[k] for k in aiec_layer_keys]

  def get_skname_for_bilo(self, bilo, sid=0):
    """
    return superkernel for buffer info layer
    """
    aiec_layers = self.get_aiec_layers_by_bilo(bilo)
    if aiec_layers:
      core = f"{sid*self.cps}_0"
      if aiec_layers[0]["core_information"].get(core):
        return aiec_layers[0]["core_information"][core]["kernel_name"]
      else:
        print(f"[WARNING] MLADF Info for core {core} at Layer_{bilo} not found")
    return ""

  def _get_iters_for_bilo(self, bilo):
    """
    find iters for a layer
    """
    aiec_layers = self.get_aiec_layers_by_bilo(bilo)
    if not aiec_layers:
      return 1
    iters = 0
    for aiec_layer in aiec_layers:
      iters += aiec_layer["core_information"]["0_0"]["kernel_repetition"]
    return iters

  def get_elfid_for_bilo(self, bilo, sid):
    """
    Find elf ID for buffer info layer order + stamp id
    """
    aiec_layers = self.get_aiec_layers_by_bilo(bilo)
    if not aiec_layers:
      return -1

    core = f"{sid*self.cps}_0"
    pm_info = {}
    if aiec_layers[0]["core_information"].get(core):
      pm_info = aiec_layers[0]["core_information"][core]["pm_information"]
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

      flexml_match = re.search(r'(flexml_layers\[\d+\])', inst)
      if flexml_match:
        parents.add(flexml_match.group(1))
        continue

      flexml_flat = re.search(r'flexml_layer_(\d+)', inst)
      if flexml_flat:
        parents.add(f"flexml_layers[{flexml_flat.group(1)}]")
        continue

      parts = inst.split(".")
      found = False
      for part in parts:
        if re.search(r'_layer_\d+', part) and "_mk[" not in part:
          parent = re.sub(r'_layer_\d+$', '', part)
          parents.add(parent)
          found = True
          break

      if not found and len(parts) >= 2:
        candidate = re.sub(r'^compute_graph\.', '', inst).split(".")[0]
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
          outer = re.sub(r'_layer_\d+(?:_\d+)*(?:\[\d+\])?$', '', part)
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
    stripped = re.sub(r'^compute_graph\.', '', name)
    parent = re.sub(r'_layer_\d+$', '', stripped)
    return parent

  def _approach1_map(self, bi_layers, m2_layers):
    """
    Map each m2 layer to exactly one buffer_info layer via parent graph name.
    """
    bi_parents = {}
    for _, bi_layer in bi_layers.items():
      bi_key = bi_layer["layer_order"]
      parents = set()
      for obj_name in bi_layer.get("layer_object_name", []):
        parents.add(self._extract_parent_graph(obj_name))
      bi_parents[bi_key] = parents

    m2_parents = {}
    for m2_key, m2_layer in m2_layers.items():
      kernel_str = m2_layer.get("kernel_node_instances", "")
      m2_parents[m2_key] = self._extract_m2_parent_graphs(kernel_str)

    bi_to_m2 = {}
    for bi_key, bi_pgraphs in bi_parents.items():
      bi_to_m2[bi_key] = []
      for m2_key, m2_pgraphs in m2_parents.items():
        overlap = m2_pgraphs & bi_pgraphs
        if overlap:
          bi_to_m2[bi_key].append(m2_key)

    return bi_to_m2

