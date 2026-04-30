# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Advanced Micro Devices, Inc. All rights reserved.

"""
AIE Status Guidance Checker
Evaluates AIE status against predefined rules and provides guidance for debugging
"""

import json
import pathlib
from typing import Dict, List, Tuple, Any, Optional
from enum import Enum

# AIE Core Status Register Bit Masks
# These correspond to bit positions in the CORE_STATUS register
# NOTE: These are the same for all AIE generations
CORE_STATUS_ENABLED_MASK = 0x1    # Bit 0: Core enabled/disabled
CORE_STATUS_RESET_MASK = 0x2      # Bit 1: Core in reset state

# Severity levels for categorizing guidance rule failures
class Severity(Enum):
  """Severity levels for guidance messages"""
  ERROR = "error"
  WARNING = "warning"
  INFO = "info"

# Result of a single guidance rule evaluation with pass/fail status and details
class GuidanceResult:
  """Result of a single guidance rule check"""

  def __init__(self, rule_id: str, rule_name: str, category: str, subcategory: str,
               passed: bool, severity: Severity, message: str, guidance: str,
               tile_location: Optional[Tuple[int, int]] = None,
               actual_value: Any = None, expected_value: Any = None):
    self.rule_id = rule_id
    self.rule_name = rule_name
    self.category = category
    self.subcategory = subcategory
    self.passed = passed
    self.severity = severity
    self.message = message
    self.guidance = guidance
    self.tile_location = tile_location
    self.actual_value = actual_value
    self.expected_value = expected_value

  def __str__(self) -> str:
    """String representation of guidance result"""
    status = "PASS" if self.passed else self.severity.value.upper()
    location = f" [{self.tile_location[0]},{self.tile_location[1]}]" if self.tile_location else ""
    return f"[{status}]{location} {self.message}"

  def to_dict(self) -> Dict[str, Any]:
    """Convert to dictionary for JSON serialization"""
    return {
      "rule_id": self.rule_id,
      "rule_name": self.rule_name,
      "category": self.category,
      "subcategory": self.subcategory,
      "passed": self.passed,
      "severity": self.severity.value,
      "message": self.message,
      "guidance": self.guidance if not self.passed else "",
      "tile_location": self.tile_location,
      "actual_value": str(self.actual_value) if self.actual_value is not None else None,
      "expected_value": str(self.expected_value) if self.expected_value is not None else None
    }


# Main guidance checker that evaluates AIE status against configurable rules
class AIEGuidanceChecker:
  """
  Checks AIE status data against guidance rules and provides actionable feedback
  """

  def __init__(self, rules_file: Optional[str] = None, aie_iface=None):
    """
    Initialize the guidance checker with rules from JSON file

    Args:
      rules_file: Path to JSON file containing guidance rules.
                  If None, uses default rules file in mldebug package.
      aie_iface: Architecture interface for device-specific definitions (optional)
    """
    if rules_file is None:
      # Use default rules file in mldebug package
      rules_file = pathlib.Path(__file__).parent / "aie_guidance_rules.json"

    self.rules = self._load_rules(rules_file)
    self.results: List[GuidanceResult] = []
    self.aie_iface = aie_iface  # Store architecture interface

  # Load guidance rules from JSON configuration file
  def _load_rules(self, rules_file: str) -> Dict[str, Dict]:
    """Load guidance rules from JSON file"""
    try:
      with open(rules_file, 'r', encoding="utf-8") as f:
        data = json.load(f)
        # Convert list of rules to dict keyed by rule_id
        return {rule['id']: rule for rule in data['rules']}
    except FileNotFoundError:
      print(f"[WARNING] Guidance rules file not found: {rules_file}")
      return {}
    except json.JSONDecodeError as e:
      print(f"[ERROR] Failed to parse guidance rules file: {e}")
      return {}

  # Evaluate a single guidance rule against actual hardware status value
  def _evaluate_rule(self, rule: Dict, actual_value: Any, col = None,
                     row = None, extra_params = None) -> GuidanceResult:
    """
    Evaluate a single rule against actual value

    Args:
      rule: Rule definition dict
      actual_value: Actual value from status data
      col: Tile column (optional)
      row: Tile row (optional)
      extra_params: Additional parameters for message formatting

    Returns:
      GuidanceResult object
    """
    threshold = rule['threshold']
    operator = rule['operator']
    value_type = rule['value_type']

    # Convert actual_value to appropriate type
    if value_type == 'int':
      actual_value = int(actual_value) if actual_value is not None else 0
      threshold = int(threshold)
    elif value_type == 'float':
      actual_value = float(actual_value) if actual_value is not None else 0.0
      threshold = float(threshold)
    elif value_type == 'bool':
      if isinstance(actual_value, bool):
        pass  # Already boolean
      elif isinstance(actual_value, str):
        actual_value = actual_value.lower() in ('true', '1', 'enabled', 'yes')
      else:
        actual_value = bool(actual_value)
      threshold = bool(threshold)
    # For string type, keep as-is

    # Evaluate based on operator
    passed = False
    if operator == '==':
      passed = actual_value == threshold
    elif operator == '!=':
      passed = actual_value != threshold
    elif operator == '>':
      passed = actual_value > threshold
    elif operator == '>=':
      passed = actual_value >= threshold
    elif operator == '<':
      passed = actual_value < threshold
    elif operator == '<=':
      passed = actual_value <= threshold

    # Build message with parameter substitution
    message = rule['good_message'] if passed else rule['bad_message']
    params = {'col': col, 'row': row, 'value': actual_value}
    if extra_params:
      params.update(extra_params)

    try:
      message = message.format(**params)
    except KeyError:
      # If formatting fails, use message as-is
      pass

    severity = Severity.ERROR if rule.get('severity', 'error') == 'error' else \
               Severity.WARNING if rule.get('severity') == 'warning' else Severity.INFO

    return GuidanceResult(
      rule_id=rule['id'],
      rule_name=rule['name'],
      category=rule['category'],
      subcategory=rule['subcategory'],
      passed=passed,
      severity=severity,
      message=message,
      guidance=rule['guidance'],
      tile_location=(col, row) if col is not None and row is not None else None,
      actual_value=actual_value,
      expected_value=threshold
    )

  # Check AIE core tile status (enabled, PC, locks, error events)
  def check_core_status(self, status_data: Dict) -> None:
    """
    Check core status data against guidance rules

    Args:
      status_data: Dictionary of AIE status data from AIEStatus.results
    """
    aie_tile_key = None
    # Find the AIE tile type key (may vary: 'aie_tile', 'AIE_TILE_T', etc.)
    for key in status_data.keys():
      if 'aie' in key.lower() and 'tile' in key.lower():
        aie_tile_key = key
        break

    if not aie_tile_key or aie_tile_key not in status_data:
      return

    tile_data = status_data[aie_tile_key]

    # Check CORE_STATUS
    if 'CORE_STATUS' in tile_data:
      for entry in tile_data['CORE_STATUS']:
        _, col, row, status_val, __, parsed = entry

        # Parse status value to extract enabled, reset, running flags
        # This is device-specific, but typically bits indicate these states
        status_int = int(status_val, 16) if isinstance(status_val, str) else status_val

        # Check if core is enabled
        if 'CORE_ENABLED' in self.rules:
          enabled = bool(status_int & CORE_STATUS_ENABLED_MASK)
          result = self._evaluate_rule(self.rules['CORE_ENABLED'], enabled, col, row)
          self.results.append(result)

        # Check if core is in reset
        if 'CORE_IN_RESET' in self.rules:
          in_reset = bool(status_int & CORE_STATUS_RESET_MASK)
          result = self._evaluate_rule(self.rules['CORE_IN_RESET'], in_reset, col, row)
          self.results.append(result)

        # Check if core is in lock stall - look for "Lock_Stall" in parsed status
        if 'CORE_LOCK_STALL' in self.rules and parsed:
          lock_stall = 'Lock_Stall' in parsed or 'LOCK_STALL' in parsed.upper()
          result = self._evaluate_rule(self.rules['CORE_LOCK_STALL'], lock_stall, col, row)
          self.results.append(result)

        # Check if core is in error halt - look for "Error_Halt" in parsed status
        if 'CORE_ERROR_HALT' in self.rules and parsed:
          error_halt = 'Error_Halt' in parsed or 'ERROR_HALT' in parsed.upper()
          result = self._evaluate_rule(self.rules['CORE_ERROR_HALT'], error_halt, col, row)
          self.results.append(result)

    # Check lock overflows/underflows
    if 'LOCK_OFL' in tile_data and 'LOCK_OVERFLOW' in self.rules:
      for entry in tile_data['LOCK_OFL']:
        _, col, row, value, __, parsed = entry
        overflow_count = int(value, 16) if isinstance(value, str) else value
        if overflow_count > 0:
          result = self._evaluate_rule(self.rules['LOCK_OVERFLOW'], overflow_count, col, row)
          self.results.append(result)

    if 'LOCK_UFL' in tile_data and 'LOCK_UNDERFLOW' in self.rules:
      for entry in tile_data['LOCK_UFL']:
        _, col, row, value, __, parsed = entry
        underflow_count = int(value, 16) if isinstance(value, str) else value
        if underflow_count > 0:
          result = self._evaluate_rule(self.rules['LOCK_UNDERFLOW'], underflow_count, col, row)
          self.results.append(result)

    # Check event status for errors - only check registers defined in architecture
    if 'EVENT_STATUS_ERRORS' in self.rules and self.aie_iface:
      # Get the error event register names from architecture
      error_regs = [self.aie_iface.ERRORS_EVENT_REG]
      if hasattr(self.aie_iface, 'ERRORS_EVENT_REG2'):
        error_regs.append(self.aie_iface.ERRORS_EVENT_REG2)

      # Get the specific error event strings to check for
      error_strings = self.aie_iface.errors_event_strings if hasattr(self.aie_iface, 'errors_event_strings') else []

      # Check each error register
      for error_reg_name in error_regs:
        if error_reg_name in tile_data:
          for entry in tile_data[error_reg_name]:
            _, col, row, value, __, parsed = entry

            # Check if any of the specific error strings are in the parsed output
            errors_found = []
            for error_str in error_strings:
              if error_str in parsed:
                errors_found.append(error_str)

            # Only report if specific errors from errors_event_strings are found
            if errors_found:
              event_val = int(value, 16) if isinstance(value, str) else value
              # Create custom message with specific errors
              error_list = ", ".join(errors_found)
              result = self._evaluate_rule(
                self.rules['EVENT_STATUS_ERRORS'],
                event_val,
                col,
                row,
                {'errors': error_list}
              )
              # Override message to include specific errors
              result.message = result.message.replace('Status: {value}', f'Errors: {error_list}')
              self.results.append(result)

  # Check DMA channel status for activity and configuration
  def check_dma_status(self, status_data: Dict) -> None:
    """
    Check DMA status data against guidance rules

    Args:
      status_data: Dictionary of AIE status data
    """
    # Placeholder for future DMA-specific guidance rules
    # Currently no active DMA rules

  # Check shim tile status (microcontroller firmware, DMA configuration)
  def check_shim_status(self, status_data: Dict) -> None:
    """
    Check shim tile status data against guidance rules

    Args:
      status_data: Dictionary of AIE status data
    """
    shim_tile_key = None
    for key in status_data.keys():
      if 'shim' in key.lower() and 'tile' in key.lower():
        shim_tile_key = key
        break

    if not shim_tile_key or shim_tile_key not in status_data:
      return

    tile_data = status_data[shim_tile_key]

    # Check microcontroller status
    if 'UC_STATUS' in tile_data and 'UC_FIRMWARE_RUNNING' in self.rules:
      for entry in tile_data['UC_STATUS']:
        _, col, row, uc_data = entry
        # uc_data is list of (name, value) tuples
        fw_state = None
        for name, value in uc_data:
          if 'FW_STATE' in name:
            fw_state = value
            break

        if fw_state:
          result = self._evaluate_rule(self.rules['UC_FIRMWARE_RUNNING'], fw_state, col, row)
          self.results.append(result)

    # Check if shim DMA is configured
    if ('dma_mm2s_status' in tile_data or 'dma_s2mm_status' in tile_data) and 'SHIM_DMA_CONFIGURED' in self.rules:
      for col_row_pair in set([(e[1], e[2]) for section in ['dma_mm2s_status', 'dma_s2mm_status']
                                if section in tile_data for e in tile_data[section]]):
        col, row = col_row_pair
        # If we have DMA status entries, assume DMA is configured
        configured = True  # Simplified check
        result = self._evaluate_rule(self.rules['SHIM_DMA_CONFIGURED'], configured, col, row)
        self.results.append(result)

  # Run all guidance checks (core, DMA, shim) on collected status data
  def check_all(self, status_data: Dict) -> List[GuidanceResult]:
    """
    Run all guidance checks on status data

    Args:
      status_data: Dictionary of AIE status data from AIEStatus.results

    Returns:
      List of GuidanceResult objects
    """
    self.results = []

    self.check_core_status(status_data)
    self.check_dma_status(status_data)
    self.check_shim_status(status_data)

    return self.results

  # Get summary statistics (total checks, passed, errors, warnings, info)
  def get_summary(self) -> Dict[str, int]:
    """
    Get summary statistics of guidance results

    Returns:
      Dictionary with counts of passed, errors, and warnings
    """
    summary = {
      'total': len(self.results),
      'passed': sum(1 for r in self.results if r.passed),
      'errors': sum(1 for r in self.results if not r.passed and r.severity == Severity.ERROR),
      'warnings': sum(1 for r in self.results if not r.passed and r.severity == Severity.WARNING),
      'info': sum(1 for r in self.results if not r.passed and r.severity == Severity.INFO)
    }
    return summary

  # Print guidance results to console in structured table format
  def print_results(self, show_passed: bool = False, show_guidance: bool = True) -> None:
    """
    Print guidance results to console in a structured table format

    Args:
      show_passed: If True, also show rules that passed
      show_guidance: If True, show detailed guidance for failed rules
    """
    if not self.results:
      print("[INFO] No guidance checks performed")
      return

    print("\n" + "=" * 100)
    print("AIE STATUS GUIDANCE REPORT")
    print("=" * 100)

    # Group results by category
    by_category: Dict[str, List[GuidanceResult]] = {}
    for result in self.results:
      cat = result.category
      if cat not in by_category:
        by_category[cat] = []
      by_category[cat].append(result)

    # Print results grouped by category as tables
    for category, cat_results in sorted(by_category.items()):
      # Filter based on show_passed
      display_results = [r for r in cat_results if not r.passed or show_passed]
      if not display_results:
        continue

      print(f"\nSection: {category.upper()}")
      print("-" * 100)

      # Table header
      header = f"{'Status':<8} | {'Location':<10} | {'Rule':<35} | {'Message':<40}"
      print(header)
      print("-" * 100)

      # Table rows
      for result in display_results:
        # Status column
        if result.passed:
          status = "PASS"
        elif result.severity == Severity.ERROR:
          status = "ERROR"
        elif result.severity == Severity.WARNING:
          status = "WARNING"
        else:
          status = "INFO"

        # Location column
        if result.tile_location:
          location = f"[{result.tile_location[0]},{result.tile_location[1]}]"
        else:
          location = "N/A"

        # Rule name (truncate if too long for table)
        rule_name = result.rule_name[:33] + ".." if len(result.rule_name) > 35 else result.rule_name

        # Print row with full message (no truncation)
        row = f"{status:<8} | {location:<10} | {rule_name:<35} | {result.message}"
        print(row)

        # Show guidance and values if requested and failed
        if show_guidance and not result.passed:
          print(f"         | {'':10} | {'':35} | → {result.guidance}")
          if result.actual_value is not None:
            print(f"         | {'':10} | {'':35} |   Actual: {result.actual_value}, Expected: {result.expected_value}")

      print()

    # Print summary at the end
    summary = self.get_summary()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Total Checks:   {summary['total']}")
    print(f"  Passed:       {summary['passed']}")
    print(f"  Errors:       {summary['errors']}")
    print(f"  Warnings:     {summary['warnings']}")
    print(f"  Info:         {summary['info']}")
    print("=" * 100)

  # Export guidance results to JSON file for external processing
  def export_json(self, filename: str) -> None:
    """
    Export guidance results to JSON file

    Args:
      filename: Output filename
    """
    output = {
      'summary': self.get_summary(),
      'results': [r.to_dict() for r in self.results]
    }

    with open(filename, 'w', encoding="utf-8") as f:
      json.dump(output, f, indent=2)

    print(f"[INFO] Guidance results exported to {filename}")
