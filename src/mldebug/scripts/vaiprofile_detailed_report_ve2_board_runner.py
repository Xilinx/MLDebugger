#!/usr/bin/env python3
"""Run VAIML tests on a PetaLinux board with the Detailed Profile Report flow (on by default;
--no-detailed-profiling turns it off).

Input is either --test-path or --archive. --test-path is a single test folder
or a container folder with test folders at any depth (e.g. the flattened layout
written by copy_rai_design_subset.py --tar). A test folder is one holding
onnx_model/ or vitisai_config.json; container folders are searched recursively
without entering test folders, hidden folders or *.partial folders. Tests are
named by their path relative to --test-path (a single test by its basename).
--archive is a tar or zip that is extracted (by default next to it, into a
folder named after it) and then used as --test-path. ve2.py is taken from --runner, else the
nearest one per test: the test folder, its parents up to --test-path, then, for
a single test, its parent.

Setup, done once (the pip3 commands are skipped with --skip-setup):
  pip3 config set global.trusted-host "..."; pip3 install torch==2.5.1 onnx pandas torchvision==0.20.1 ...
  LD_LIBRARY_PATH, XRT_AIARM, XLNX_ENABLE_CACHE, ENABLE_ML_DEBUG, QEMU_TEST,
  DEBUG_VAIML_PARTITION and FLEXMLRT_LOG_LEVEL are set for every run.
Then, also once and even with --skip-setup,
  pip3 list | grep -iE 'flexmlrt|voe|onnxruntime[-_]vitisai'
checks the VAI packages (pip may list onnxruntime-vitisai as onnxruntime_vitisai).
If any is missing, the first whl_files/ folder found (every test folder found,
in order, then --test-path, then, for a single test, its parent) is used to run
  pip3 install flexmlrt onnxruntime_vitisai voe --no-deps --force-reinstall --find-links ./whl_files/
from that folder. If there is no whl_files/, the install fails, or a package is
still missing afterwards, the script stops before running any test.

For each test, from its own folder, the overlay is reloaded first:
  fpgautil -R
  fpgautil -b ./overlay/x_plus_ml.pdi -o ./overlay/x_plus_ml.dtbo
If <test>/overlay/ or either file is missing, a warning is printed, fpgautil is
not run and the test's runs still go ahead. If fpgautil -R fails, a warning is
printed and fpgautil -b still runs. If fpgautil -b fails, the test is an ERROR
and none of its runs are done.

Each test is then run once from its own folder:
  python ve2.py --input_model onnx_model/<model>.onnx --analyzer_profiling True
                [--vaiml_custom_ops <test>/custom_ops_config.json]
If vaiml_config.custom_ops is non-empty in vitisai_config.json or
vitisai_config.json.orig, <test>/custom_ops_config.json must exist and is
passed with --vaiml_custom_ops; if it is missing the test is an ERROR and none
of its runs are done.
If vitisai_config.json lists control_instrumentation in
vaiml_config.ai_analyzer_enhanced_profiling, the base run uses it with
control_instrumentation removed from that list. vitisai_config.json is restored
if the base run passes; otherwise it is left as it is, and the original is kept
as vitisai_config.json.with_control_instrumentation.

Unless --no-detailed-profiling is given, a warning is printed if
control_instrumentation is not found in vitisai_config.json, or else in
vitisai_config.json.with_control_instrumentation or vitisai_config.json.orig
(those two are only checked, never used), and each test whose base run passes
is run 4 more times anyway. Before the first of these runs, if the test folder
holds aie_dtrace_ctx_*.ct, dtrace_dump_ctx_*.json or vai_profile*.zip files, they
are backed up once into <test>/backup_vaiprofile_<current_timestamp>.zip, with
<current_timestamp> as YYYYmmdd_HHMMSS: existing vai_profile*.zip files are moved
aside, vaiprofile-collect-data <test>/ is run if dtrace files are present (log in
<test>/vaiprofile-collect-data_backup.log) and its vai_profile.zip becomes the
backup, then the old vai_profile*.zip files are added into the backup and removed
from the test folder. If the collect fails, a warning is printed and the runs go
ahead. Each of these runs uses
vitisai_config.json with control_instrumentation added to
vaiml_config.ai_analyzer_enhanced_profiling and
vaiml_config.profiling_runtime_config set to one of PROFILE_RUNS. Each run gets
<test>/vaiprofile_reports/<run folder>/ holding the vitisai_config.json it used,
its board_run.log, vaiprofile-collect-data.log and the vai_profile.zip made by
vaiprofile-collect-data <test>/, saved as vaiprofile_data.zip. vitisai_config.json
is restored afterwards.

Writes per-test logs, <test-path>/setup.log and <test-path>/vaiprofile_detailed_run_summary.csv.
Exits non-zero if any base run or any profiling run did not pass.
"""

import argparse
import copy
import csv
import fnmatch
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

RUNNER = 've2.py'
CONFIG = 'vitisai_config.json'
CONFIG_ORIG = CONFIG + '.orig'
CONFIG_BACKUP = CONFIG + '.dpr_backup'
CONFIG_WITH_CI = CONFIG + '.with_control_instrumentation'
CI_WARNING = ('"control_instrumentation" is not present in "vaiml_config"."ai_analyzer_enhanced_profiling". '
              'Please check whether the design is compiled with "control_instrumentation". Otherwise, detailed '
              'profiling data cannot be captured.')
CUSTOM_OPS_CONFIG = 'custom_ops_config.json'
BASE_LOG = 'board_run.log'
SETUP_LOG = 'setup.log'
PROFILE_DIR = 'vaiprofile_reports'
COLLECT = 'vaiprofile-collect-data'
COLLECT_ZIP = 'vai_profile.zip'
REPORT_ZIP = 'vaiprofile_data.zip'
COLLECT_LOG = 'vaiprofile-collect-data.log'
BACKUP_COLLECT_LOG = 'vaiprofile-collect-data_backup.log'
DTRACE_PATTERNS = ('aie_dtrace_ctx_*.ct', 'dtrace_dump_ctx_*.json')
OLD_ZIP_PATTERN = 'vai_profile*.zip'
BACKUP_TRIGGERS = DTRACE_PATTERNS + (OLD_ZIP_PATTERN,)
KILL_WAIT = 30
SETUP_CMDS = (
    ['pip3', 'config', 'set', 'global.trusted-host',
     'pypi.org files.pythonhosted.org pypi.python.org download.pytorch.org'],
    ['pip3', 'install', 'torch==2.5.1', 'onnx', 'pandas', 'torchvision==0.20.1',
     '--extra-index-url', 'https://download.pytorch.org/whl/cpu'],
)
VAI_PACKAGES = ('flexmlrt', 'voe', 'onnxruntime-vitisai')
VAI_CHECK_CMD = "pip3 list | grep -iE 'flexmlrt|voe|onnxruntime[-_]vitisai'"
WHL_DIR = 'whl_files'
VAI_INSTALL_CMD = ['pip3', 'install', 'flexmlrt', 'onnxruntime_vitisai', 'voe', '--no-deps',
                   '--force-reinstall', '--find-links', './whl_files/']
OVERLAY_FILES = ('overlay/x_plus_ml.pdi', 'overlay/x_plus_ml.dtbo')
FPGA_CMDS = (['fpgautil', '-R'],
             ['fpgautil', '-b', './overlay/x_plus_ml.pdi', '-o', './overlay/x_plus_ml.dtbo'])
FPGA_LOG = 'fpgautil.log'
LD_PATHS = ('/usr/lib/python3.12/site-packages/flexmlrt/lib/', '/usr/lib/python3.12/site-packages/voe/lib/')
ENV = (('XRT_AIARM', 'true'), ('XLNX_ENABLE_CACHE', '0'), ('ENABLE_ML_DEBUG', '0'), ('QEMU_TEST', '0'),
       ('DEBUG_VAIML_PARTITION', '2'), ('FLEXMLRT_LOG_LEVEL', 'debug'))
PROFILE_RUNS = (
    ('A', 'detailed_core_shim_read_ch_0',
     {'interface_tile': 'detailed_ddr_read_bandwidth:0', 'aie_tile': 'compute_io_bound'}),
    ('B', 'detailed_shim_read_ch_1', {'interface_tile': 'detailed_ddr_read_bandwidth:1'}),
    ('C', 'detailed_shim_write_ch_0', {'interface_tile': 'detailed_ddr_write_bandwidth:0'}),
    ('D', 'detailed_shim_write_ch_1', {'interface_tile': 'detailed_ddr_write_bandwidth:1'}),
)
ARCHIVE_EXTS = ('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar', '.zip')
HELP_DESCRIPTION = '''\
Run VAIML tests on a PetaLinux board with the Detailed Profile Report flow (on by default;
--no-detailed-profiling turns it off).
Detailed profiling data captured from hardware is stored in "vaiprofile_reports" sub-directory for each test.
To generate Detailed Profile Report with Boundness Classification, please copy the "vaiprofile_reports"
sub-directory for each test to Linux x86 for further analysis with "vaiprofile" tool using
"--generate-profile-report" switch.'''


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, 'Usage: ' if prefix is None else prefix)


HIDDEN_HELP = {
    'archive': 'Tar (optionally compressed) or zip holding one test or many; it is extracted and the extracted '
               'folder is used as --test-path',
    'extract_dir': 'Where to extract --archive (default: next to the archive, named after it without the '
                   'extension); must not exist yet',
}


class HelpAllAction(argparse.Action):
    """--help-all: the --help text with the full description after the short one, plus the hidden options."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        full = __doc__.strip().split('\n\n', 1)[1]
        text = parser.format_help().replace(HELP_DESCRIPTION, HELP_DESCRIPTION + '\n\n' + full, 1)
        self_help = copy.copy(self)
        self_help.help = 'Show this help message with the full description and hidden options, and exit'
        hidden = [self_help]
        for action in parser._actions:
            if action.dest in HIDDEN_HELP:
                action = copy.copy(action)
                action.help = HIDDEN_HELP[action.dest]
                hidden.append(action)
        formatter = parser._get_formatter()
        formatter.start_section('Hidden Options')
        formatter.add_arguments(hidden)
        formatter.end_section()
        parser._print_message(text + '\n' + formatter.format_help(), sys.stdout)
        parser.exit()


BANNER_WIDTH = 80
BANNER_LEAD = 8


def banner(title, prefix=''):
    """Print '<prefix>======== title ====...', BANNER_WIDTH wide including prefix; never cuts the title."""
    head = '%s%s %s ' % (prefix, '=' * BANNER_LEAD, title)
    print('%s%s' % (head, '=' * max(BANNER_WIDTH - len(head), BANNER_LEAD)))
    sys.stdout.flush()


def banner_end():
    print('=' * BANNER_WIDTH)
    sys.stdout.flush()


def archive_stem(path):
    base = os.path.basename(path)
    for ext in ARCHIVE_EXTS:
        if base.lower().endswith(ext):
            return base[:-len(ext)]
    return os.path.splitext(base)[0]


def extract_archive(archive, dest):
    """Extract a tar or zip into dest (which must not exist). Return the folder to use as --test-path."""
    if os.path.exists(dest):
        raise ValueError('%s already exists; delete it, pass a different --extract-dir, or run with '
                         '--test-path %s' % (dest, dest))
    tmp = dest + '.partial'
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive, 'r:*') as tf:
                if hasattr(tarfile, 'data_filter'):
                    tf.extractall(tmp, filter='data')
                else:
                    base = os.path.realpath(tmp) + os.sep
                    members = [m for m in tf.getmembers() if (m.isfile() or m.isdir())
                               and os.path.realpath(os.path.join(tmp, m.name)).startswith(base)]
                    tf.extractall(tmp, members)
        else:
            raise ValueError('%s is not a tar or zip archive' % archive)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    os.rename(tmp, dest)
    entries = os.listdir(dest)
    if len(entries) == 1:
        only = os.path.join(dest, entries[0])
        if os.path.isdir(only) and not is_test_dir(only):
            return only
    return dest


def is_test_dir(path):
    return os.path.isdir(os.path.join(path, 'onnx_model')) or os.path.isfile(os.path.join(path, CONFIG))


def find_test_dirs(test_path):
    """Return (single, [(name, test_dir)]).

    If test_path is itself a test folder it is the only test (named after its
    basename). Otherwise test folders are found at any depth below it, named by
    their path relative to test_path; the walk does not enter test folders,
    hidden folders or *.partial folders, and does not follow symlinks.
    """
    if is_test_dir(test_path):
        return True, [(os.path.basename(test_path), test_path)]
    found = []
    for cur, dirs, _ in os.walk(test_path):
        dirs.sort()
        keep = []
        for d in dirs:
            if d.startswith('.') or d.endswith('.partial'):
                continue
            path = os.path.join(cur, d)
            if is_test_dir(path):
                found.append((os.path.relpath(path, test_path), path))
            else:
                keep.append(d)
        dirs[:] = keep
    return False, found


def discover(test_dirs):
    """Return (tests, skipped): tests are (name, test_dir, model_file); skipped are (name, reason)."""
    tests, skipped = [], []
    for name, path in test_dirs:
        models = sorted(glob.glob(os.path.join(path, 'onnx_model', '*.onnx')))
        if not models:
            skipped.append((name, 'no onnx_model/*.onnx'))
        elif not os.path.isfile(os.path.join(path, CONFIG)):
            skipped.append((name, 'no %s' % CONFIG))
        else:
            if len(models) > 1:
                print('WARNING %s: several onnx_model/*.onnx; using %s' % (name, os.path.basename(models[0])))
            tests.append((name, path, os.path.basename(models[0])))
    return tests, skipped


def find_runner(test_dir, test_path, single):
    """Return the nearest ve2.py: test folder, its parents up to test_path, then (single test) its parent."""
    dirs, cur = [], test_dir
    while True:
        dirs.append(cur)
        if cur == test_path or os.path.dirname(cur) == cur:
            break
        cur = os.path.dirname(cur)
    if single:
        dirs.append(os.path.dirname(test_path))
    return next((os.path.join(d, RUNNER) for d in dirs if os.path.isfile(os.path.join(d, RUNNER))), None)


def load_json(path):
    with open(path) as fh:
        return json.load(fh)


def vaiml_configs(cfg):
    passes = cfg.get('passes', []) if isinstance(cfg, dict) else []
    return [p['vaiml_config'] for p in passes if isinstance(p, dict) and isinstance(p.get('vaiml_config'), dict)]


def has_control_instrumentation(cfg):
    return any('control_instrumentation' in (vc.get('ai_analyzer_enhanced_profiling') or [])
               for vc in vaiml_configs(cfg))


def custom_ops_file(test_dir, cfg):
    """Return (path for --vaiml_custom_ops or None, error or None).

    custom_ops counts as used when any vaiml_config.custom_ops is non-empty in cfg
    (vitisai_config.json) or in a readable vitisai_config.json.orig.
    """
    used_in = [CONFIG] if any(vc.get('custom_ops') for vc in vaiml_configs(cfg)) else []
    try:
        orig = load_json(os.path.join(test_dir, CONFIG_ORIG))
    except (OSError, ValueError):
        orig = None
    if orig is not None and any(vc.get('custom_ops') for vc in vaiml_configs(orig)):
        used_in.append(CONFIG_ORIG)
    if not used_in:
        return None, None
    path = os.path.join(test_dir, CUSTOM_OPS_CONFIG)
    if os.path.isfile(path):
        return path, None
    return None, 'custom_ops set in %s but %s not found' % (' and '.join(used_in), CUSTOM_OPS_CONFIG)


def control_instrumentation_found(test_dir, cfg):
    """True if cfg (vitisai_config.json), else a readable vitisai_config.json.with_control_instrumentation
    or vitisai_config.json.orig, lists control_instrumentation."""
    if has_control_instrumentation(cfg):
        return True
    for name in (CONFIG_WITH_CI, CONFIG_ORIG):
        try:
            if has_control_instrumentation(load_json(os.path.join(test_dir, name))):
                return True
        except (OSError, ValueError):
            pass
    return False


def with_control_instrumentation(cfg):
    cfg = copy.deepcopy(cfg)
    for vc in vaiml_configs(cfg):
        enhanced = vc.get('ai_analyzer_enhanced_profiling')
        if not isinstance(enhanced, list):
            vc['ai_analyzer_enhanced_profiling'] = ['control_instrumentation']
        elif 'control_instrumentation' not in enhanced:
            vc['ai_analyzer_enhanced_profiling'] = enhanced + ['control_instrumentation']
    return cfg


def without_control_instrumentation(cfg):
    cfg = copy.deepcopy(cfg)
    for vc in vaiml_configs(cfg):
        if isinstance(vc.get('ai_analyzer_enhanced_profiling'), list):
            vc['ai_analyzer_enhanced_profiling'] = [p for p in vc['ai_analyzer_enhanced_profiling']
                                                    if p != 'control_instrumentation']
    return cfg


def with_runtime_config(cfg, runtime):
    cfg = copy.deepcopy(cfg)
    for vc in vaiml_configs(cfg):
        vc['profiling_runtime_config'] = {'control_instrumentation': dict(runtime)}
    return cfg


def restore_stale_backup(test_dir, tag, dry_run):
    """Put back a vitisai_config.json.dpr_backup left by an interrupted run."""
    backup = os.path.join(test_dir, CONFIG_BACKUP)
    if not os.path.isfile(backup):
        return
    if dry_run:
        print('WARNING %s: %s left by an earlier run; it would be restored as %s' % (tag, CONFIG_BACKUP, CONFIG))
        return
    print('WARNING %s: %s left by an earlier run; restoring it as %s first' % (tag, CONFIG_BACKUP, CONFIG))
    os.replace(backup, os.path.join(test_dir, CONFIG))


def run_step(cmd, cwd, log_path, timeout, append=False):
    """Run cmd with output to log_path. Return (exit code or None, timeout status or None, seconds)."""
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    start = time.time()
    with open(log_path, 'a' if append else 'w') as log:
        log.write('Command: %s\n' % subprocess.list2cmdline(cmd))
        log.flush()
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)
        try:
            return proc.wait(timeout=timeout), None, time.time() - start
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError as e:
                print('WARNING: could not kill PID %d: %s' % (proc.pid, e))
            try:
                proc.wait(timeout=KILL_WAIT)
                status = 'TIMEOUT'
            except subprocess.TimeoutExpired:
                status = 'TIMEOUT (process not killed)'
            return None, status, time.time() - start


def remove_outputs(test_dir):
    for f in os.listdir(test_dir):
        if fnmatch.fnmatch(f, 'ofm_*.npy') and not f[:-len('.npy')].endswith('_ref'):
            os.remove(os.path.join(test_dir, f))


def inference(cmd, test_dir, log_path, timeout):
    """Run ve2.py. Return (status, exit code, seconds); PASS needs exit code 0 and ofm_0.npy."""
    remove_outputs(test_dir)
    rc, timed_out, secs = run_step(cmd, test_dir, log_path, timeout)
    if timed_out:
        return timed_out, '', secs
    if rc != 0:
        return 'FAIL', rc, secs
    if not os.path.isfile(os.path.join(test_dir, 'ofm_0.npy')):
        return 'FAIL (no ofm_0.npy)', rc, secs
    return 'PASS', rc, secs


def base_run(cmd, test_dir, cfg, timeout, tag):
    """Run the base inference. Return (status, exit code, seconds, note or None).

    If cfg (vitisai_config.json) lists control_instrumentation, the run uses it with
    control_instrumentation removed. vitisai_config.json is restored if the run passes (or never
    ran); otherwise it is left as it is and the original is moved to vitisai_config.json.with_control_instrumentation.
    """
    log_path = os.path.join(test_dir, BASE_LOG)
    if not has_control_instrumentation(cfg):
        return inference(cmd, test_dir, log_path, timeout) + (None,)
    config = os.path.join(test_dir, CONFIG)
    backup = os.path.join(test_dir, CONFIG_BACKUP)
    shutil.copy2(config, backup)
    status = None
    try:
        with open(config, 'w') as fh:
            json.dump(without_control_instrumentation(cfg), fh, indent=4)
        print('%s: Base Run with control_instrumentation removed from %s' % (tag, CONFIG))
        sys.stdout.flush()
        status, rc, secs = inference(cmd, test_dir, log_path, timeout)
    finally:
        if status in (None, 'PASS'):
            os.replace(backup, config)
    if status == 'PASS':
        return status, rc, secs, None
    os.replace(backup, os.path.join(test_dir, CONFIG_WITH_CI))
    note = ('base run without control_instrumentation failed; control_instrumentation not restored in %s '
            '(original kept as %s)' % (CONFIG, CONFIG_WITH_CI))
    print('%s: %s' % (tag, note[0].upper() + note[1:]))
    return status, rc, secs, note


def profile_test(test_dir, cmd, base_cfg, timeout, tag):
    """Run the PROFILE_RUNS captures. Return {run id: status}."""
    prof_dir = os.path.join(test_dir, PROFILE_DIR)
    if os.path.isdir(prof_dir):
        shutil.rmtree(prof_dir)
    os.makedirs(prof_dir)
    config = os.path.join(test_dir, CONFIG)
    backup = os.path.join(test_dir, CONFIG_BACKUP)
    shutil.copy2(config, backup)
    results = {}
    try:
        for run_id, run_name, runtime in PROFILE_RUNS:
            if results and any(s.startswith('TIMEOUT (process not killed)') for s in results.values()):
                results[run_id] = 'not_run (earlier process not killed)'
                continue
            run_dir = os.path.join(prof_dir, run_name)
            os.makedirs(run_dir)
            with open(config, 'w') as fh:
                json.dump(with_runtime_config(base_cfg, runtime), fh, indent=4)
            shutil.copy2(config, os.path.join(run_dir, CONFIG))
            print('%s: Profiling Run %s %s (%s) ...' % (tag, run_id, run_name,
                                                        ', '.join('%s=%s' % kv for kv in runtime.items())))
            sys.stdout.flush()
            status, _, secs = inference(cmd, test_dir, os.path.join(run_dir, BASE_LOG), timeout)
            if status != 'PASS':
                results[run_id] = status
            else:
                results[run_id] = collect(test_dir, run_dir, timeout)
            print('%s: Profiling Run %s %s (%.1f s)' % (tag, run_id, results[run_id], secs))
    finally:
        os.replace(backup, config)
    return results


def matching_files(test_dir, patterns):
    return sorted(p for pattern in patterns for p in glob.glob(os.path.join(test_dir, pattern)) if os.path.isfile(p))


def run_backup_collect(test_dir, timeout):
    """Run vaiprofile-collect-data on test_dir, leaving vai_profile.zip there. Return an error or None."""
    log = os.path.join(test_dir, BACKUP_COLLECT_LOG)
    if shutil.which(COLLECT) is None:
        return '%s not on PATH' % COLLECT
    rc, timed_out, _ = run_step([COLLECT, test_dir.rstrip('/') + '/'], test_dir, log, timeout)
    if timed_out:
        return '%s %s (log %s)' % (COLLECT, timed_out, log)
    if rc != 0:
        return '%s failed (rc %s) (log %s)' % (COLLECT, rc, log)
    zip_path = os.path.join(test_dir, COLLECT_ZIP)
    if not os.path.isfile(zip_path) or os.path.getsize(zip_path) == 0:
        return '%s made no non-empty %s (log %s)' % (COLLECT, COLLECT_ZIP, log)
    return None


def backup_collect(test_dir, timeout):
    """Make backup_vaiprofile_<current_timestamp>.zip (YYYYmmdd_HHMMSS) in test_dir.

    Existing vai_profile*.zip files are moved aside first, so vaiprofile-collect-data cannot overwrite them.
    If dtrace files are present, vaiprofile-collect-data is run and its vai_profile.zip becomes the backup.
    The old vai_profile*.zip files are then added into the backup and removed from test_dir.
    Return (backup path or None, error or None).
    """
    dest = os.path.join(test_dir, 'backup_vaiprofile_%s.zip' % time.strftime('%Y%m%d_%H%M%S'))
    old_zips = matching_files(test_dir, (OLD_ZIP_PATTERN,))
    stash = None
    if old_zips:
        stash = tempfile.mkdtemp(prefix='.vai_profile_backup_', dir=test_dir)
        for path in old_zips:
            os.replace(path, os.path.join(stash, os.path.basename(path)))
    err = None
    try:
        if matching_files(test_dir, DTRACE_PATTERNS):
            err = run_backup_collect(test_dir, timeout)
            if err is None:
                os.replace(os.path.join(test_dir, COLLECT_ZIP), dest)
        if stash:
            with zipfile.ZipFile(dest, 'a' if os.path.isfile(dest) else 'w', zipfile.ZIP_DEFLATED) as zf:
                for name in sorted(os.listdir(stash)):
                    zf.write(os.path.join(stash, name), name)
            shutil.rmtree(stash)
    except BaseException:
        if stash and os.path.isdir(stash):
            for name in os.listdir(stash):
                os.replace(os.path.join(stash, name), os.path.join(test_dir, name))
            shutil.rmtree(stash, ignore_errors=True)
        raise
    return (dest if os.path.isfile(dest) else None), err


def collect(test_dir, run_dir, timeout):
    """Run vaiprofile-collect-data on test_dir and move vai_profile.zip into run_dir as vaiprofile_data.zip.
    Return a status."""
    log = os.path.join(run_dir, COLLECT_LOG)
    if shutil.which(COLLECT) is None:
        with open(log, 'w') as fh:
            fh.write('ERROR: %s not found on PATH\n' % COLLECT)
        return 'FAIL (%s not on PATH)' % COLLECT
    rc, timed_out, _ = run_step([COLLECT, test_dir.rstrip('/') + '/'], test_dir, log, timeout)
    if timed_out:
        return 'collect ' + timed_out
    if rc != 0:
        return 'collect FAIL (rc %s)' % rc
    zip_path = os.path.join(test_dir, COLLECT_ZIP)
    if not os.path.isfile(zip_path) or os.path.getsize(zip_path) == 0:
        return 'collect FAIL (no non-empty %s)' % COLLECT_ZIP
    shutil.move(zip_path, os.path.join(run_dir, REPORT_ZIP))
    return 'PASS'


def setup(test_path, skip, dry_run, timeout):
    """Start <test_path>/setup.log, set the environment and run SETUP_CMDS (unless skip). Return an error or None."""
    open(os.path.join(test_path, SETUP_LOG), 'w').close()
    os.environ['LD_LIBRARY_PATH'] = ':'.join(LD_PATHS + tuple(p for p in os.environ.get('LD_LIBRARY_PATH', '')
                                                                   .split(':') if p))
    for k, v in ENV:
        os.environ[k] = v
    for k in ('LD_LIBRARY_PATH',) + tuple(k for k, _ in ENV):
        print('export %s=%s' % (k, os.environ[k]))
    if skip:
        print('Setup: pip3 commands skipped (--skip-setup)')
        return None
    if dry_run:
        for cmd in SETUP_CMDS:
            print('Setup: %s' % subprocess.list2cmdline(cmd))
        return None
    log_path = os.path.join(test_path, SETUP_LOG)
    for cmd in SETUP_CMDS:
        print('Setup: %s' % subprocess.list2cmdline(cmd))
        sys.stdout.flush()
        err = run_logged(cmd, test_path, log_path, timeout)
        if err:
            return err
    return None


def run_logged(cmd, cwd, log_path, timeout):
    """Run cmd appending to log_path. Return None on exit code 0, else an error message."""
    name = subprocess.list2cmdline(cmd)
    try:
        rc, timed_out, _ = run_step(cmd, cwd, log_path, timeout, append=True)
    except OSError as e:
        return '%s: %s (log %s)' % (name, e, log_path)
    if timed_out:
        return '%s: %s (log %s)' % (name, timed_out, log_path)
    if rc != 0:
        return '%s failed (rc %s) (log %s)' % (name, rc, log_path)
    return None


def missing_vai_packages(log_path, timeout):
    """Run VAI_CHECK_CMD (output appended to log_path). Return the VAI_PACKAGES it does not list."""
    try:
        out = subprocess.run(VAI_CHECK_CMD, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        out = ''
    with open(log_path, 'a') as log:
        log.write('Command: %s\n%s' % (VAI_CHECK_CMD, out))
    listed = {line.split()[0].lower().replace('_', '-') for line in out.splitlines() if line.split()}
    return [p for p in VAI_PACKAGES if p not in listed]


def ensure_vai_packages(test_path, test_dirs, single, dry_run, timeout):
    """Install VAI_PACKAGES from the first whl_files/ found if any is missing. Return an error or None.

    whl_files/ is looked for in test_dirs in order, then test_path, then (single test) its parent.
    """
    log_path = os.path.join(test_path, SETUP_LOG)
    print('Setup: %s' % VAI_CHECK_CMD)
    missing = missing_vai_packages(log_path, timeout)
    if not missing:
        print('%s already installed' % ', '.join(VAI_PACKAGES))
        return None
    dirs = list(test_dirs) + [test_path] + ([os.path.dirname(test_path)] if single else [])
    src = next((d for d in dirs if os.path.isdir(os.path.join(d, WHL_DIR))), None)
    if src is None:
        return '%s not installed (missing: %s) and no %s/ found in any test folder or in %s' % (
            ', '.join(VAI_PACKAGES), ', '.join(missing), WHL_DIR,
            ' or '.join([test_path] + ([os.path.dirname(test_path)] if single else [])))
    print('Missing %s; Installing from %s' % (', '.join(missing), os.path.join(src, WHL_DIR)))
    print('Setup: (cd %s) %s' % (src, subprocess.list2cmdline(VAI_INSTALL_CMD)))
    if dry_run:
        return None
    sys.stdout.flush()
    err = run_logged(VAI_INSTALL_CMD, src, log_path, timeout)
    if err:
        return 'installing from %s: %s' % (os.path.join(src, WHL_DIR), err)
    missing = missing_vai_packages(log_path, timeout)
    if missing:
        return 'still missing after installing from %s: %s (log %s)' % (
            os.path.join(src, WHL_DIR), ', '.join(missing), log_path)
    print('%s installed' % ', '.join(VAI_PACKAGES))
    return None


def missing_overlay(test_dir):
    """Return a description of what is missing from <test>/overlay/, or None if OVERLAY_FILES are all there."""
    overlay_dir = os.path.dirname(OVERLAY_FILES[0])
    if not os.path.isdir(os.path.join(test_dir, overlay_dir)):
        return '%s/ not found' % overlay_dir
    missing = [f for f in OVERLAY_FILES if not os.path.isfile(os.path.join(test_dir, f))]
    return '%s not found' % ' and '.join(missing) if missing else None


def load_overlay(test_dir, timeout):
    """Run FPGA_CMDS from test_dir, logged to <test>/fpgautil.log.

    Return (warning or None, error or None): fpgautil -R failing is a warning, fpgautil -b failing is an error.
    """
    log_path = os.path.join(test_dir, FPGA_LOG)
    open(log_path, 'w').close()
    reset_cmd, load_cmd = FPGA_CMDS
    warning = run_logged(reset_cmd, test_dir, log_path, timeout)
    return warning, run_logged(load_cmd, test_dir, log_path, timeout)


def run_one_test(name, test_dir, model, tag, test_runner, profiling, args):
    """Run (or, with --dry-run, print) the base run and profiling runs of one test. Return its summary row."""
    row = {'test': name, 'log': os.path.join(test_dir, BASE_LOG)}
    if test_runner is None:
        row.update(status='SKIPPED', note='no %s in the test folder or its parents (use --runner)' % RUNNER)
        print('SKIP %s: %s' % (tag, row['note']))
        return row
    restore_stale_backup(test_dir, tag, args.dry_run)
    try:
        cfg = load_json(os.path.join(test_dir, CONFIG))
    except (OSError, ValueError) as e:
        row.update(status='SKIPPED', note='cannot read %s: %s' % (CONFIG, e))
        print('SKIP %s: %s' % (tag, row['note']))
        return row
    cmd = [sys.executable, test_runner, '--input_model', 'onnx_model/' + model, '--analyzer_profiling', 'True']
    ops, err = custom_ops_file(test_dir, cfg)
    if err:
        row['status'] = 'ERROR (%s)' % err
        print('ERROR %s: %s; no runs done' % (tag, err))
        return row
    if ops:
        cmd += ['--vaiml_custom_ops', ops]
    overlay_missing = missing_overlay(test_dir)
    if overlay_missing:
        msg = '%s; fpgautil skipped' % overlay_missing
        row['note'] = msg
        print('WARNING %s: %s' % (tag, msg))
    if profiling and not control_instrumentation_found(test_dir, cfg):
        row['note'] = '; '.join(n for n in (row.get('note'), CI_WARNING) if n)
        print('WARNING %s: %s' % (tag, CI_WARNING))
    base_cfg = with_control_instrumentation(cfg) if profiling else None
    if profiling:
        row['profile'] = 'on'

    if args.dry_run:
        print('  cwd: %s' % test_dir)
        if overlay_missing:
            print('  (fpgautil skipped: %s)' % overlay_missing)
        else:
            for fcmd in FPGA_CMDS:
                print('  %s >> %s' % (subprocess.list2cmdline(fcmd), FPGA_LOG))
        print('  %s > %s' % (subprocess.list2cmdline(cmd), BASE_LOG))
        if has_control_instrumentation(cfg):
            print('  (Base Run: control_instrumentation removed from %s; restored only if the Base Run '
                  'passes)' % CONFIG)
        if base_cfg is not None:
            print('  Backup Collect (only if %s present): %s %s/ > %s (only if %s present); '
                  'mv %s backup_vaiprofile_<current_timestamp>.zip; add %s into it' % (
                      ' or '.join(BACKUP_TRIGGERS), COLLECT, test_dir, BACKUP_COLLECT_LOG,
                      ' or '.join(DTRACE_PATTERNS), COLLECT_ZIP, OLD_ZIP_PATTERN))
            for run_id, run_name, runtime in PROFILE_RUNS:
                run_dir = os.path.join(PROFILE_DIR, run_name)
                print('  Profiling Run %s: %s with control_instrumentation and '
                      'vaiml_config.profiling_runtime_config = %s, copied to %s/' % (
                          run_id, CONFIG, json.dumps({'control_instrumentation': runtime}), run_dir))
                print('    %s > %s/%s; %s %s/ > %s/%s; mv %s %s/%s' % (
                    subprocess.list2cmdline(cmd), run_dir, BASE_LOG, COLLECT, test_dir, run_dir, COLLECT_LOG,
                    COLLECT_ZIP, run_dir, REPORT_ZIP))
        row['status'] = 'DRY_RUN'
        return row

    if not overlay_missing:
        print('%s: Reloading Overlay (fpgautil -R, fpgautil -b) ...' % tag)
        sys.stdout.flush()
        warning, err = load_overlay(test_dir, args.timeout)
        if warning:
            msg = '%s; continuing' % warning
            row['note'] = '; '.join(n for n in (row.get('note'), msg) if n)
            print('WARNING %s: %s' % (tag, msg))
        if err:
            row['status'] = 'ERROR (%s)' % err
            row.pop('profile', None)
            print('ERROR %s: %s; no runs done' % (tag, err))
            return row
    print('%s: Running Inference ...' % tag)
    sys.stdout.flush()
    status, rc, secs, note = base_run(cmd, test_dir, cfg, args.timeout, tag)
    row.update(status=status, base_rc=rc, base_s='%.1f' % secs)
    if note:
        row['note'] = '; '.join(n for n in (row.get('note'), note) if n)
    print('%s: Base Run %s (%.1f s)' % (tag, status, secs))
    if base_cfg is None:
        return row
    if status != 'PASS':
        row['profile'] = 'not_run (base run not PASS)'
        return row
    if matching_files(test_dir, BACKUP_TRIGGERS):
        print('%s: Doing backup collect data before detailed profiling' % tag)
        sys.stdout.flush()
        backup, err = backup_collect(test_dir, args.timeout)
        if err:
            msg = 'backup %s failed: %s; continuing with profiling runs' % (COLLECT, err)
            print('WARNING %s: %s' % (tag, msg))
            row['note'] = '; '.join(n for n in (row.get('note'), msg) if n)
        if backup:
            print('%s: Backup Collect saved %s' % (tag, backup))
    else:
        print('%s: No %s in test folder; backup collect skipped' % (tag, ' or '.join(BACKUP_TRIGGERS)))
    pstart = time.time()
    try:
        for run_id, result in profile_test(test_dir, cmd, base_cfg, args.timeout, tag).items():
            row['run_' + run_id] = result
    except OSError as e:
        row['profile'] = 'ERROR (%s)' % e
    row['profile_s'] = '%.1f' % (time.time() - pstart)
    return row


def main():
    ap = argparse.ArgumentParser(usage='%(prog)s --test-path TEST_PATH [options]', description=HELP_DESCRIPTION,
                                 formatter_class=HelpFormatter, add_help=False)
    ap._optionals.title = 'Options'
    ap.add_argument('-h', '--help', action='help', help='Show this help message and exit')
    ap.add_argument('--help-all', action=HelpAllAction, help=argparse.SUPPRESS)
    ap.add_argument('--test-path', help='A test folder, or a folder holding test folders at any depth')
    ap.add_argument('--archive', help=argparse.SUPPRESS)
    ap.add_argument('--extract-dir', help=argparse.SUPPRESS)
    ap.add_argument('--runner', help='Path to ve2.py (default: the nearest ve2.py per test: the test folder, '
                                     'its parents up to --test-path, then, for a single test, its parent)')
    ap.add_argument('--no-detailed-profiling', action='store_true',
                    help='Only do the base run of each test; by default, after a PASS, each test is run %d more '
                         'times with detailed profiling and the data is collected in <test>/%s'
                         % (len(PROFILE_RUNS), PROFILE_DIR))
    ap.add_argument('--timeout', type=float, default=1800, help='Per-step timeout in seconds (default 1800)')
    ap.add_argument('--skip-setup', action='store_true',
                    help='Skip the pip3 config/install setup commands (environment is still set; the VAI package '
                         'check, and fpgautil per test, still run)')
    ap.add_argument('--dry-run', action='store_true', help='Print the setup and run commands; run nothing')
    args = ap.parse_args()
    profiling = not args.no_detailed_profiling

    if args.test_path and args.archive:
        ap.error('--test-path and --archive cannot be used together')
    if not args.test_path and not args.archive:
        ap.error('--test-path is required')
    if args.extract_dir and not args.archive:
        ap.error('--extract-dir needs --archive')
    print('Control Instrumentation based Detailed Profiling : %s'
          % ('ON' if profiling else 'OFF (--no-detailed-profiling)'))
    banner('Test Discovery')
    if args.archive:
        archive = os.path.abspath(args.archive)
        if not os.path.isfile(archive):
            ap.error('--archive file not found: %s' % archive)
        dest = os.path.abspath(args.extract_dir or os.path.join(os.path.dirname(archive), archive_stem(archive)))
        print('Extracting %s to %s ...' % (archive, dest))
        sys.stdout.flush()
        try:
            test_path = extract_archive(archive, dest)
        except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as e:
            ap.error('cannot extract %s: %s' % (archive, e))
    else:
        test_path = os.path.abspath(args.test_path)
        if not os.path.isdir(test_path):
            ap.error('--test-path is not a directory: %s' % test_path)
    single, test_dirs = find_test_dirs(test_path)
    print('Test path: %s (%s)' % (test_path, 'single test' if single else 'container'))
    runner = None
    if args.runner:
        runner = os.path.abspath(args.runner)
        if not os.path.isfile(runner):
            ap.error('--runner file not found: %s' % runner)
    print('Runner: %s' % ('--runner %s' % runner if runner else 'nearest %s per test' % RUNNER))
    tests, skipped = discover(test_dirs)
    rows = [{'test': n, 'status': 'SKIPPED', 'note': r} for n, r in skipped]
    found = len(test_dirs)
    print('Found %d test folder%s%s in %s: %d to run, %d skipped' % (
        found, '' if found == 1 else 's', ' (single test)' if single else '',
        'archive %s' % archive if args.archive else test_path, len(tests), len(skipped)))
    for i, (name, _, _) in enumerate(tests, 1):
        print('  %d. %s' % (i, name))
    for name, reason in skipped:
        print('SKIP %s: %s' % (name, reason))
    if not tests:
        print('ERROR: no tests found in %s' % test_path)
        banner_end()
        return 1
    banner_end()

    banner('Setup Board and Environment')
    err = setup(test_path, args.skip_setup, args.dry_run, args.timeout)
    if err:
        print('ERROR: setup failed: %s' % err)
        banner_end()
        return 1
    err = ensure_vai_packages(test_path, [d for _, d in test_dirs], single, args.dry_run, args.timeout)
    if err:
        print('ERROR: %s' % err)
        banner_end()
        return 1
    banner_end()

    for i, (name, test_dir, model) in enumerate(tests, 1):
        prefix = '[%d/%d] ' % (i, len(tests))
        banner(name, prefix)
        test_runner = runner or find_runner(test_dir, test_path, single)
        rows.append(run_one_test(name, test_dir, model, prefix + name, test_runner, profiling, args))
        banner_end()

    run_cols = ['run_' + r for r, _, _ in PROFILE_RUNS]
    fields = ['test', 'status', 'base_rc', 'base_s', 'profile'] + run_cols + ['profile_s', 'log', 'note']
    summary = os.path.join(test_path, 'vaiprofile_detailed_run_summary.csv')
    with open(summary, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=fields, restval='')
        w.writeheader()
        w.writerows(rows)

    banner('Summary')
    print('%-50s %-22s %s' % ('Test', 'Status', 'Notes'))
    failed = 0
    for r in rows:
        detail = [r.get('note', '')]
        if profiling:
            profile = r.get('profile', '')
            if profile:
                detail.append('Detailed Profile : %s' % ('ON' if profile == 'on' else profile))
            detail += ['Run %s %s' % (c[4:], r[c]) for c in run_cols if r.get(c)]
        print('%-50s %-22s %s' % (r['test'], r['status'], '; '.join(d for d in detail if d)))
        bad_profile = profiling and r['status'] in ('PASS', 'DRY_RUN') and (
            r.get('profile', '').startswith('ERROR') or any(r.get(c, 'PASS') != 'PASS' for c in run_cols))
        if r['status'] not in ('PASS', 'DRY_RUN') or bad_profile:
            failed += 1
    print('\n%d/%d without failures. Summary: %s' % (len(rows) - failed, len(rows), summary))
    if args.dry_run:
        print('Dry run: nothing executed')
    banner_end()
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
