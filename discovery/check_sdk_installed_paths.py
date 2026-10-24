#!/usr/bin/env python3
"""Host controls for installed-byte capture without CoreSimulator path RPCs."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import shutil
import unittest

from check_sdk_m0 import fixture, write_trace
from sdk_m0_conformance import load
from sdk_installed_paths import record
from summarize import EvidenceError

ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = '15BD0984-9F42-4CE5-B096-365FB38F735F'


def installed_fixture(root):
    captures = root / 'captures'; captures.mkdir(); fixture(captures)
    applications = root / 'Library/Developer/CoreSimulator/Devices' / SIMULATOR / 'data/Containers/Bundle/Application'
    for key, name, bundle in [('example', 'sdk-m0', 'dev.iosvoipsdk.example'),
                              ('bootstrap', 'bootstrap-healthy', 'dev.callkitsim.sdkbootstrap')]:
        app = applications / ('AAAAAAAA-AAAA-4AAA-8AAA-' + ('A' if key == 'example' else 'B') * 12) / (key + '.app')
        app.mkdir(parents=True)
        (app / 'Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier=bundle, CFBundleExecutable=key)))
        (app / key).write_bytes((captures / 'binaries' / key).read_bytes())
        if key == 'example': (app / 'main.jsbundle').write_bytes((captures / 'binaries/main.jsbundle').read_bytes())
        names = [name] if key == 'example' else ['bootstrap-' + case for case in ('healthy', 'profile', 'push-after-install')]
        for trace_name in names:
            rows = load(captures, trace_name)
            for row in rows:
                row.update(simulatorID=SIMULATOR)
                if row['event'] == 'launch': row['bundlePath'] = str(app)
            write_trace(captures, trace_name, rows)
            receipt = captures / (trace_name + '.receipt.json')
            value = json.loads(receipt.read_text()); value['simulatorID'] = SIMULATOR
            receipt.write_text(json.dumps(value))
    return captures


class InstalledPathsTests(unittest.TestCase):
    def test_infrastructure_log_preserves_stdout_and_failure_status(self):
        source = (ROOT / 'discovery/run_sdk_m0.sh').read_text()
        wrapper = source[source.index('limit() {'):source.index('\ncleanup_simulator()')]
        command = wrapper + '\nlimit 2 /bin/sh -c \'printf value; exit 73\'\n'
        result = subprocess.run(['/bin/bash', '-c', command], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 73)
        self.assertEqual(result.stdout, 'value')
        self.assertIn('SDK infrastructure start (2s): /bin/sh', result.stderr)
        self.assertIn('SDK infrastructure exit: 73', result.stderr)

    def test_actual_runner_no_longer_depends_on_installed_path_rpc(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            captures = installed_fixture(root)
            binary = root / 'bin'; binary.mkdir()
            xcrun = binary / 'xcrun'
            xcrun.write_text('#!/bin/sh\n[ "$1" = simctl ] && [ "$2" = install ] && exit 0\nexit 142\n')
            xcrun.chmod(0o755)
            python = binary / 'python3'
            python.write_text('#!' + sys.executable + '\n' + '''import os,pathlib,shutil,sys
if sys.argv[1] == 'discovery/run_scenario.py':
    args=sys.argv[2:]
    def arg(key): return args[args.index(key)+1]
    name=arg('--scenario'); source=pathlib.Path(os.environ['FIXTURE_CAPTURES'])
    shutil.copyfile(source/(name+'.log'),arg('--output'))
    shutil.copyfile(source/(name+'.receipt.json'),arg('--receipt'))
else: os.execv(sys.executable,[sys.executable,*sys.argv[1:]])
''')
            python.chmod(0o755)
            output = root / 'out'; output.mkdir()
            source = (ROOT / 'discovery/run_sdk_m0.sh').read_text()
            start = source.index('limit 180 xcrun simctl install "$UDID" "$SDK_M0_APP"')
            end = source.index('if [[ "${SDK_M1_ACCEPTANCE:-0}" == 1 ]]; then', start)
            environment = dict(os.environ, PATH=str(binary) + ':/usr/bin:/bin', OUT=str(output),
                UDID=SIMULATOR, RUN_ID='run-1', INHERITED_DYLD='', SDK_M0_APP='source-example',
                SDK_M0_BOOTSTRAP_APP='source-bootstrap', FIXTURE_CAPTURES=str(captures))
            result = subprocess.run(['/bin/bash', '-c', 'set -euo pipefail\nlimit() { shift; "$@"; }\n' + source[start:end]],
                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((output / 'installed.json').read_text()),
                             json.loads((captures / 'installed.json').read_text()))


    def test_refuses_unbound_or_escaped_installed_paths(self):
        cases = ('missing-path', 'foreign-simulator', 'relative-path', 'symlink-path',
                 'wrong-bundle', 'executable-traversal', 'symlink-image', 'mixed-run', 'wrong-pid', 'trace-hash')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                captures = installed_fixture(root)
                rows = load(captures, 'sdk-m0')
                launch = next(row for row in rows if row['event'] == 'launch')
                app = Path(launch['bundlePath'])
                if case == 'missing-path': launch.pop('bundlePath')
                elif case == 'foreign-simulator':
                    foreign = Path(str(app).replace(SIMULATOR, '26CE0A95-B3EB-4CD7-A1A7-476FB49F846A'))
                    shutil.copytree(app, foreign)
                    launch['bundlePath'] = str(foreign)
                elif case == 'relative-path': launch['bundlePath'] = 'relative/example.app'
                elif case == 'symlink-path':
                    alias = app.with_name('alias.app'); alias.symlink_to(app, target_is_directory=True)
                    launch['bundlePath'] = str(alias)
                elif case == 'wrong-bundle':
                    (app / 'Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier='foreign', CFBundleExecutable='example')))
                elif case == 'executable-traversal':
                    (app / 'Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier='dev.iosvoipsdk.example', CFBundleExecutable='../foreign')))
                elif case == 'symlink-image':
                    outside = root / 'foreign'; outside.write_bytes(b'foreign')
                    (app / 'example').unlink(); (app / 'example').symlink_to(outside)
                elif case == 'mixed-run': launch['runID'] = 'another-run'
                write_trace(captures, 'sdk-m0', rows)
                receipt = captures / 'sdk-m0.receipt.json'
                value = json.loads(receipt.read_text()); value['simulatorID'] = SIMULATOR
                if case == 'wrong-pid': value['launcherAppPID'] = 999
                if case == 'trace-hash': value['traceSHA256'] = 'f' * 64
                receipt.write_text(json.dumps(value))
                (captures / 'installed.json').unlink()
                with self.assertRaises((EvidenceError, ValueError)):
                    record(captures, 'run-1', SIMULATOR)
                self.assertFalse((captures / 'installed.json').exists())


if __name__ == '__main__': unittest.main()
