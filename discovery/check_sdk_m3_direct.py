#!/usr/bin/env python3
"""Host controls for the explicitly direct A9 evidence boundary; no simulator."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import check_sdk_m0 as common
import run_sdk_m3_direct as runner
import sdk_m3_direct_conformance as gate
from summarize import EvidenceError

SUPERVISOR, LAUNCHER = 7001, 7002


def write_direct_trace(root, rows, run_id, simulator):
    common.write_trace(root, gate.NAME, rows)
    path = root / (gate.NAME + '.log')
    path.write_text(gate.BUNDLE + ': 123\n' + path.read_text())
    common.change_json(root, gate.NAME + '.receipt.json', lambda value: value.update(
        runID=run_id, simulatorID=simulator, traceSHA256=gate.m0.digest(path),
        launcherPID=LAUNCHER, terminationMethod='console-launcher-signal', terminationSignal=15,
        terminationSignalDelivered=True, consoleTransport='host-pty', appAbsenceMethod='signal-zero',
        appAbsentAfterStop=True, inheritLauncherGroup=True, supervisorPID=SUPERVISOR,
        supervisorPGID=SUPERVISOR, supervisorSID=SUPERVISOR, launcherPGID=SUPERVISOR))
    common.change_json(root, gate.NAME + '.receipt.json', lambda value: value.pop('terminateExitCode'))


def macho(claims=None):
    xml = plistlib.dumps({'com.apple.developer.calling-app': True} if claims is None else claims)
    header = struct.pack('<8I', 0xFEEDFACF, 0x100000C, 0, 2, 1, 152, 0, 0)
    segment = bytearray(152)
    struct.pack_into('<II', segment, 0, 0x19, 152)
    struct.pack_into('<I', segment, 64, 1)
    segment[72:88] = b'__entitlements'.ljust(16, b'\0')
    segment[88:104] = b'__TEXT'.ljust(16, b'\0')
    struct.pack_into('<Q', segment, 112, len(xml))
    struct.pack_into('<I', segment, 120, len(header) + len(segment))
    return header + segment + xml


def sample():
    control = dict(schemaVersion=1, runID='run', simulatorID='sim', appPID=123, delivery=gate.DELIVERY)
    control_bytes = (json.dumps(control, indent=2) + '\n').encode()
    manifest = dict(runID='run', simulatorID='sim', directControlSHA256=hashlib.sha256(control_bytes).hexdigest(),
                    directControlCreatedWallTimeMS=1_790_000_000_065)
    rows = []
    events = [
        ('app.entry', {}), ('launch', dict(mode='sdk-m3-direct', traceVersion=2)),
        ('sdk.activation', dict(active=True)), ('sdk.managerCreated', dict(owner='VoIPApplication.start')),
        ('sdk.snapshot', dict(conversations=[])), ('m3.listenerReady', dict(conversationCount=0)),
        ('m3.directControl', dict(conversationCount=0, controlSHA256=manifest['directControlSHA256'])),
    ]
    for kind, method, raw, handle, video in gate.STEPS:
        events.append(('m3.directDelivery', dict(kind=kind, method=method, inputSHA256=hashlib.sha256(raw.encode()).hexdigest())))
        events.append(('m3.nativeIngress', dict(kind='url', urlSHA256=hashlib.sha256(raw.encode()).hexdigest())) if kind == 'url'
                      else ('m3.directIntentEntered', dict(isStartCallIntent=True)))
        events.append(('m3.directDeliveryReturned', dict(kind=kind, handled=True)))
        events.append(('m3.dialRequest', dict(conversationCount=0, handleSHA256=hashlib.sha256(handle.encode()).hexdigest(), video=video)))
    events += [('sdk.result', dict(success=True)), ('done', {})]
    for index, (event, fields) in enumerate(events):
        rows.append(dict(event=event, sequence=index + 1, t_ms=index * 10,
                         wallTimeMS=1_790_000_000_000 + index * 10, pid=123, runID='run', simulatorID='sim',
                         bundleID=gate.BUNDLE, main=True, **fields))
    # The external control is written between readiness and its native read.
    manifest['directControlCreatedWallTimeMS'] = rows[5]['wallTimeMS'] + 5
    return rows, control, manifest


def fixture(root):
    (root / 'binaries').mkdir()
    (root / 'binaries/example').write_bytes(macho())
    (root / 'binaries/main.jsbundle').write_bytes(b'synthetic direct fixture JS')
    (root / 'Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier=gate.BUNDLE, UIBackgroundModes=['voip'])))
    (root / 'simulated-entitlements.plist').write_bytes(gate.simulated_entitlements(root / 'binaries/example'))
    (root / 'host-entitlements.plist').write_bytes(plistlib.dumps({}))
    (root / 'build-logs').mkdir(); (root / 'build-logs/build.log').write_bytes(b'synthetic build only')
    identity = common.IDENTITY
    common.write_json(root / 'input-build-receipt.json', dict(schemaVersion=1, product='sdk-m3', configuration='Release',
        mediaAdapter='none', **identity, sourceStart=identity, sourceEnd=identity,
        files={name: gate.m0.digest(root / name) for name in ('binaries/example', 'binaries/main.jsbundle', 'Info.plist')},
        buildLogs={'build.log': gate.m0.digest(root / 'build-logs/build.log')}))
    common.write_json(root / 'environment.json', dict(simulatorID='sim', simulatorRuntimeID='ios-27', simulatorOS='27.0',
        simulatorBuild='synthetic', xcodeVersion='27.1'))
    common.write_json(root / 'cleanup.json', dict(simulatorID='sim', deleted=True, absentFromInventory=True,
        globalEnvironmentUnchanged=True, supervisorPID=SUPERVISOR, supervisorCleanupRequired=False,
        supervisorGroupAbsentAfterStop=True, forcedGroupKill=False))
    common.write_json(root / 'commands.json', dict(steps=[dict(phase=phase, exitCode=0) for phase in
        ('verify-built-signature', 'read-host-entitlements', 'install', 'app-data')]))
    rows, control, manifest = sample()
    (root / 'direct-control.json').write_text(json.dumps(control, indent=2) + '\n')
    write_direct_trace(root, rows, 'run', 'sim')
    manifest.update(schemaVersion=1, scenario='sdk-m3-direct', delivery=gate.DELIVERY, ownsSimulator=True,
        runtimeInjectionRequested=False, directIngressInjectionRequested=True, supervisorPID=SUPERVISOR, **identity,
        files={name: gate.m0.digest(root / name) for name in gate.FILES},
        installed={key: gate.m0.digest(root / name) for key, name in [('executableSHA256', 'binaries/example'),
            ('javascriptSHA256', 'binaries/main.jsbundle'), ('infoPlistSHA256', 'Info.plist')]})
    for name, key in [('input-build-receipt.json', 'inputBuildReceiptSHA256'), ('cleanup.json', 'cleanupSHA256'),
                      ('commands.json', 'commandsSHA256'), ('environment.json', 'environmentSHA256')]:
        manifest[key] = gate.m0.digest(root / name)
    common.write_json(root / 'manifest.json', manifest)


class Controls(unittest.TestCase):
    def run_capture(self, *, omit_receipt=False, terminate_during_boot=False,
                    dyld_status=0, dyld=b'', dyld_stderr=b'', final_dyld=None,
                    launch_dyld='/private/fixture-injector.dylib', create_timeout=False, create_invalid=False,
                    supervisor_mode='normal', host_metadata_requires_idle=False):
        with tempfile.TemporaryDirectory(prefix='sdk-m3-direct-runner-controls-') as directory:
            root = Path(directory); app = root / 'Original.app'; app.mkdir()
            data = root / 'data'; data.mkdir(); (data / 'tmp').mkdir(); out = root / 'capture'
            info = dict(CFBundleExecutable='Example', CFBundleIdentifier=gate.BUNDLE,
                        UIBackgroundModes=['voip'], NSUserActivityTypes=['INStartCallIntent'])
            (app / 'Info.plist').write_bytes(plistlib.dumps(info))
            original = macho(); (app / 'Example').write_bytes(original); (app / 'main.jsbundle').write_bytes(b'JS')
            (root / 'build.log').write_bytes(b'synthetic native build process boundary')
            receipt = root / 'build.json'
            common.write_json(receipt, dict(schemaVersion=1, product='sdk-m3', configuration='Release', mediaAdapter='none',
                **common.IDENTITY, sourceStart=common.IDENTITY, sourceEnd=common.IDENTITY,
                files={'binaries/example': gate.m0.digest(app / 'Example'),
                       'binaries/main.jsbundle': gate.m0.digest(app / 'main.jsbundle'),
                       'Info.plist': gate.m0.digest(app / 'Info.plist')},
                buildLogs={'build.log': gate.m0.digest(root / 'build.log')}))
            simulator = '00000000-0000-4000-8000-000000000001'
            calls, launches, group_calls, waits, queries = [], [], [], [], []
            device_state = dict(name=None, deleted=False)

            def external(argv, **kwargs):
                calls.append(argv); output = b''; status = 0; stderr = b''
                if argv[:3] in (['xcrun', 'simctl', 'create'], ['xcrun', 'simctl', 'boot']):
                    self.assertFalse(any(key.startswith(('SIMCTL_CHILD_SDK_M3_', 'SIMCTL_CHILD_SDK_M1_', 'SIMCTL_CHILD_SDK_SYSTEM_')) for key in kwargs['env']))
                if terminate_during_boot and argv[:3] == ['xcrun', 'simctl', 'boot']:
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                if argv[0] == 'codesign':
                    self.assertNotIn('--sign', argv)
                    if '--display' in argv: output = plistlib.dumps({})
                elif argv[:4] == ['xcrun', 'simctl', 'list', 'runtimes']:
                    output = json.dumps({'runtimes': [dict(platform='iOS', isAvailable=True,
                        identifier='ios-27', version='27.0', buildversion='fixture')]}).encode()
                elif argv[:3] == ['xcrun', 'simctl', 'create']:
                    device_state['name'] = argv[3]
                    if create_timeout: raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
                    output = b'all' if create_invalid else simulator.encode()
                elif argv[:4] == ['xcrun', 'simctl', 'list', 'devices']:
                    rows = [dict(name='sdk-m3-unrelated-QA', udid='00000000-0000-4000-8000-000000000002')]
                    if not device_state['deleted']: rows.append(dict(name=device_state['name'], udid=simulator))
                    output = json.dumps({'devices': {'ios-27': rows}}).encode()
                elif argv[:3] == ['xcrun', 'simctl', 'delete']: device_state['deleted'] = True
                elif argv[:3] == ['xcrun', 'simctl', 'spawn']:
                    self.assertEqual(argv[4:7], ['launchctl', 'getenv', 'DYLD_INSERT_LIBRARIES'])
                    self.assertEqual(kwargs['env']['SIMCTL_CHILD_DYLD_INSERT_LIBRARIES'], '')
                    self.assertEqual(kwargs['timeout'], 180)
                    values = final_dyld if queries and final_dyld is not None else (dyld_status, dyld, dyld_stderr)
                    queries.append(values)
                    status, output, stderr = values
                elif argv[:3] == ['xcrun', 'simctl', 'get_app_container']:
                    output = str(out / 'example.app' if argv[-1] == 'app' else data).encode()
                elif argv == ['xcodebuild', '-version']:
                    if host_metadata_requires_idle and device_state['name'] is not None:
                        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
                    output = b'Xcode fixture'
                elif argv[:3] not in (['xcrun', 'simctl', 'boot'], ['xcrun', 'simctl', 'bootstatus'],
                        ['xcrun', 'simctl', 'install'], ['xcrun', 'simctl', 'shutdown'], ['xcrun', 'simctl', 'terminate']):
                    raise AssertionError('Unexpected external command: ' + repr(argv))
                if argv[:3] in (['xcrun', 'simctl', 'terminate'], ['xcrun', 'simctl', 'shutdown'], ['xcrun', 'simctl', 'delete']):
                    for sig in (signal.SIGTERM, signal.SIGINT): self.assertIs(signal.getsignal(sig), signal.SIG_IGN)
                if argv[:3] == ['xcrun', 'simctl', 'shutdown']: self.assertEqual(kwargs['timeout'], 60)
                return subprocess.CompletedProcess(argv, status, output, stderr)

            class Supervisor:
                def __init__(self, argv, **kwargs):
                    if '--inherit-launcher-group' not in argv or kwargs.get('start_new_session') is not True:
                        raise AssertionError('M3 supervisor must own the inherited launcher session')
                    launch_command = argv[argv.index('--launch-command') + 1:]
                    if launch_command != ['xcrun', 'simctl', 'launch', '--console', '--terminate-running-process', simulator, gate.BUNDLE]:
                        raise AssertionError('M3 example must use the plain public console transport')
                    self.status = None; self.pid = SUPERVISOR; launches.append(kwargs['env'])
                    device_state['supervisor'] = self
                    run = kwargs['env']['SIMCTL_CHILD_SDK_M3_RUN_ID']
                    rows = sample()[0]
                    for row in rows: row.update(runID=run, simulatorID=simulator)
                    self.rows, self.run = rows, run
                    write_direct_trace(out, rows, run, simulator)
                    if omit_receipt: (out / (gate.NAME + '.receipt.json')).unlink()
                    session = data / 'Documents/.callkit-simulator'; session.mkdir(parents=True)
                    common.write_json(session / 'session.json', dict(runID=run, pid=123, bundleID=gate.BUNDLE))
                def poll(self): return self.status
                def wait(self, timeout):
                    waits.append(timeout)
                    if timeout == 90 and supervisor_mode in ('checkpoint-failure', 'timeout-live', 'unowned'):
                        raise RuntimeError('synthetic capture failure')
                    if timeout == 25 and supervisor_mode in ('timeout-live', 'unowned'):
                        raise subprocess.TimeoutExpired('synthetic supervisor', timeout)
                    if (out / 'direct-control.json').exists() and not omit_receipt:
                        control = out / 'direct-control.json'; value = json.loads(control.read_text())
                        if value != dict(schemaVersion=1, runID=self.run, simulatorID=simulator,
                                appPID=123, delivery='direct-url-and-intent'):
                            raise AssertionError('direct control identity differs')
                        if (data / 'tmp' / ('sdk-m3-direct-' + self.run + '.json')).read_bytes() != control.read_bytes():
                            raise AssertionError('direct control was not delivered to this app sandbox')
                        next(r for r in self.rows if r['event'] == 'm3.directControl')['controlSHA256'] = gate.m0.digest(control)
                        write_direct_trace(out, self.rows, self.run, simulator)
                    self.status = 0; return 0
                def terminate(self): self.status = -15
                def kill(self): self.status = -9

            def group(pgid, sig):
                self.assertEqual(pgid, SUPERVISOR); group_calls.append((pgid, sig))
                if sig == signal.SIGKILL: device_state['supervisor'].status = -9
                elif sig == 0 and supervisor_mode != 'remaining-group': raise ProcessLookupError

            environment = dict(os.environ, SIMCTL_CHILD_SDK_M3_RUN_ID='stale-boot-run',
                SIMCTL_CHILD_SDK_M1_CASE='stale-case', SIMCTL_CHILD_SDK_SYSTEM_CASE='stale-system')
            environment.pop('SIMCTL_CHILD_DYLD_INSERT_LIBRARIES', None)
            if launch_dyld is not None: environment['SIMCTL_CHILD_DYLD_INSERT_LIBRARIES'] = launch_dyld
            captured = io.StringIO()
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            registered = handlers.copy()
            def set_handler(sig, handler):
                previous = registered[sig]; registered[sig] = handler; return previous
            with patch.object(runner.m0, 'source_identity', return_value=common.IDENTITY), \
                 patch.object(runner.subprocess, 'run', side_effect=external), \
                 patch.object(runner.subprocess, 'Popen', Supervisor), \
                 patch.object(runner.os, 'getpgid', return_value=7999 if supervisor_mode == 'unowned' else SUPERVISOR), \
                 patch.object(runner.os, 'getsid', return_value=SUPERVISOR), \
                 patch.object(runner.os, 'killpg', side_effect=group), \
                 patch.object(runner.signal, 'signal', side_effect=set_handler), \
                 patch.object(runner.signal, 'getsignal', side_effect=registered.__getitem__), \
                 patch.object(runner.time, 'time_ns', return_value=1_790_000_000_055_000_000), \
                 patch.object(runner.time, 'sleep', side_effect=lambda _: None), \
                 patch.object(sys, 'argv', ['run_sdk_m3_direct.py', '--app', str(app),
                    '--build-receipt', str(receipt), '--out', str(out)]), \
                 patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(captured):
                status = runner.main()
            self.assertEqual(handlers, {sig: signal.getsignal(sig) for sig in handlers})
            self.assertEqual(handlers, registered)
            self.assertNotIn('/private/fixture-injector.dylib', captured.getvalue())
            self.assertEqual((app / 'Example').read_bytes(), original)
            cleanup = json.loads((out / 'cleanup.json').read_text())
            self.assertTrue(cleanup['deleted'])
            self.assertTrue(any(call[:3] == ['xcrun', 'simctl', 'delete'] and call[-1] == simulator for call in calls))
            self.assertFalse(any('setenv' in call or 'unsetenv' in call for call in calls))
            self.assertTrue(all(call[-1] == simulator for call in calls if call[:3] in
                (['xcrun', 'simctl', 'shutdown'], ['xcrun', 'simctl', 'delete'])))
            if create_timeout or create_invalid:
                self.assertEqual(cleanup['recoveredSimulators'], [simulator])
                self.assertTrue(cleanup['recoveredCleanup'][0]['absentFromInventory'])
                self.assertFalse(launches)
            if dyld_status not in (0, 1) or dyld_stderr or (dyld_status == 1 and dyld):
                self.assertFalse(launches, 'Invalid launch-environment query must fail before app launch')
            if launches:
                expected_dyld = launch_dyld if launch_dyld is not None else dyld.removesuffix(b'\n').decode()
                self.assertEqual(launches[0]['SIMCTL_CHILD_DYLD_INSERT_LIBRARIES'], expected_dyld)
                self.assertEqual(len(queries), 2)
                self.assertNotEqual(launches[0]['SIMCTL_CHILD_SDK_M3_RUN_ID'], 'stale-boot-run')
                self.assertEqual(launches[0]['SIMCTL_CHILD_SDK_M3_DELIVERY'], 'direct-url-and-intent')
            self.assertFalse((out / 'settings').exists())
            if host_metadata_requires_idle:
                self.assertLess(calls.index(['xcodebuild', '-version']),
                    next(index for index, argv in enumerate(calls) if argv[:3] == ['xcrun', 'simctl', 'create']))
            if launches:
                if supervisor_mode != 'unowned': self.assertIn((SUPERVISOR, 0), group_calls)
                self.assertEqual((SUPERVISOR, signal.SIGKILL) in group_calls, supervisor_mode == 'timeout-live')
                if supervisor_mode == 'checkpoint-failure': self.assertIn(25, waits)
                if supervisor_mode in ('timeout-live', 'unowned'): self.assertIn(5, waits)
            return status, json.loads((out / 'summary.json').read_text())

    def test_actual_direct_statements_against_public_simulator_interfaces(self):
        root = Path(__file__).resolve().parent.parent
        source = (root / 'example/modules/journey/apple/VoIPExampleJourneyModule.swift').read_text()
        start = source.index('    let tel = URL(string: "tel:+15555550142")!')
        end = source.index('    finish(success: true)', start)
        # Typecheck the exact production statements and real SDK entry points;
        # only fixture tracing/wait bookkeeping is substituted. No app is run.
        program = '''
import CryptoKit
import Intents
import UIKit
private enum JourneyError: Error { case invalidPayload }
@MainActor private enum JourneyTrace { static func record(_ event: String, _ fields: [String: Any]) {} }
@MainActor final class Control {
  var directDialObservations = 0
  static func digest(_ value: String) -> String { "typecheck only" }
  func waitUntil(_ condition: @MainActor () -> Bool) async throws {}
  func check() async throws {
''' + source[start:end] + '''
  }
}
'''
        with tempfile.TemporaryDirectory(prefix='sdk-m3-direct-interfaces-') as directory:
            output = Path(directory); code = output / 'Control.swift'; code.write_text(program)
            sdk = subprocess.check_output(['xcrun', '--sdk', 'iphonesimulator', '--show-sdk-path'], text=True).strip()
            result = subprocess.run(['xcrun', 'swiftc', '-typecheck', '-swift-version', '6', '-warnings-as-errors',
                '-parse-as-library', '-sdk', sdk, '-target', 'arm64-apple-ios17.4-simulator',
                '-module-cache-path', str(output / 'cache'),
                str(root / 'Sources/VoIPSDK/DialRequest.swift'), str(root / 'Sources/VoIPSDK/DialRequest+Intents.swift'),
                str(root / 'Sources/VoIPSDK/VoIPSystemEntryPoints.swift'), str(code)],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_native_control_identity_parser(self):
        program = '''
import Foundation
@main struct Control {
  static func main() throws {
    let valid = "{\\"schemaVersion\\":1,\\"runID\\":\\"run\\",\\"simulatorID\\":\\"sim\\",\\"appPID\\":123,\\"delivery\\":\\"direct-url-and-intent\\"}"
    func accepts(_ string: String, run: String = "run", sim: String = "sim", pid: Int32 = 123) -> Bool {
      M3DirectControl.matches(Data(string.utf8), runID: run, simulatorID: sim, pid: pid)
    }
    precondition(accepts(valid))
    precondition(!accepts(valid, run: "other")); precondition(!accepts(valid, sim: "other"))
    precondition(!accepts(valid, pid: 456)); precondition(!accepts("[]")); precondition(!accepts("not JSON"))
    precondition(!accepts(valid.replacingOccurrences(of: "\\"schemaVersion\\":1", with: "\\"schemaVersion\\":true")))
    precondition(!accepts(valid.replacingOccurrences(of: "\\"schemaVersion\\":1", with: "\\"schemaVersion\\":1.0")))
    precondition(!accepts(valid.replacingOccurrences(of: "\\"appPID\\":123", with: "\\"appPID\\":123.0")))
    precondition(!accepts(valid.replacingOccurrences(of: "\\"appPID\\":123", with: "\\"appPID\\":123.5")))
    precondition(!accepts(valid.replacingOccurrences(of: "direct-url-and-intent", with: "os")))
    precondition(!accepts(valid.replacingOccurrences(of: "}", with: ",\\"url\\":\\"tel:private\\"}")))
  }
}
'''
        source = Path(__file__).resolve().parent.parent / 'example/modules/journey/apple/M3DirectControl.swift'
        with tempfile.TemporaryDirectory(prefix='sdk-m3-direct-parser-') as directory:
            root = Path(directory); code = root / 'Control.swift'; code.write_text(program)
            result = subprocess.run(['xcrun', 'swiftc', '-swift-version', '6', '-warnings-as-errors', '-parse-as-library',
                '-module-cache-path', str(root / 'cache'), str(source), str(code), '-o', str(root / 'control')],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(root / 'control')], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_runner_direct_control_and_no_os_dispatch(self):
        result, summary = self.run_capture()
        self.assertEqual(result, 0, summary)
        self.assertEqual(summary['publicDialRequests'], 2)

    def test_host_metadata_precedes_simulator_load(self):
        result, summary = self.run_capture(host_metadata_requires_idle=True)
        self.assertEqual(result, 0, summary)

    def test_cold_launch_queries_preserve_unset_empty_raw_bytes_and_override(self):
        for status, value in ((1, b''), (0, b''), (0, b'/private/observed.dylib\n'),
                              (0, b'/private/observed.dylib\n\n')):
            with self.subTest(status=status, value=value):
                result, summary = self.run_capture(dyld_status=status, dyld=value, launch_dyld=None)
                self.assertEqual(result, 0, summary)
        result, summary = self.run_capture(dyld=b'/private/observed.dylib\n')
        self.assertEqual(result, 0, summary)

    def test_launch_query_diagnostics_and_unknown_status_refuse_without_launch(self):
        for status, value, stderr in ((2, b'', b''), (-15, b'', b''), (142, b'', b''),
                                      (1, b'unexpected', b''), (0, b'', b'diagnostic'), (1, b'', b'diagnostic')):
            with self.subTest(status=status, value=value, stderr=stderr):
                result, summary = self.run_capture(dyld_status=status, dyld=value, dyld_stderr=stderr)
                self.assertEqual(result, 1, summary)
                self.assertEqual(summary['failure']['reason'], 'Cannot read simulator launch environment')

    def test_legacy_and_duo_query_uses_empty_observer_and_preserves_override(self):
        source = Path(__file__).with_name('run_audio_lifecycle_legacy.sh').read_text()
        start = source.index('if [[ -n "${SIMCTL_CHILD_DYLD_INSERT_LIBRARIES+x}" ]]')
        query = source[start:source.index('SDK_AUDIO_EXTERNAL_DYLD=', start)]
        stub = '''set -euo pipefail
UDID=owned
limit() {
  [[ "$*" == '180 xcrun simctl spawn owned launchctl getenv DYLD_INSERT_LIBRARIES' &&
     "${SIMCTL_CHILD_DYLD_INSERT_LIBRARIES+x}" == x && -z "$SIMCTL_CHILD_DYLD_INSERT_LIBRARIES" ]] || return 99
  printf 'query\n' >> "$OUT/queries"
  printf '/private/observed.dylib\n'
}
'''
        for override in (None, '/private/caller.dylib'):
            with self.subTest(override=override), tempfile.TemporaryDirectory(prefix='sdk-audio-query-') as directory:
                environment = dict(os.environ, OUT=directory)
                environment.pop('SIMCTL_CHILD_DYLD_INSERT_LIBRARIES', None)
                if override is not None: environment['SIMCTL_CHILD_DYLD_INSERT_LIBRARIES'] = override
                result = subprocess.run(['/bin/bash', '-c', stub + query + 'printf "%s" "$INHERITED_DYLD"'],
                    env=environment, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, override if override is not None else '/private/observed.dylib')
                self.assertEqual((Path(directory) / 'queries').exists(), override is None)

    def test_unset_and_explicit_empty_remain_distinct_during_cleanup(self):
        for status in (0, 1):
            with self.subTest(initial_status=status):
                result, summary = self.run_capture(dyld_status=status, final_dyld=(1 - status, b'', b''))
                self.assertEqual(result, 1, summary)
                self.assertEqual(summary['failure']['reason'], 'Simulator launch environment changed during M3')

    def test_direct_runner_missing_receipt_stays_red_and_cleans(self):
        result, summary = self.run_capture(omit_receipt=True)
        self.assertEqual(result, 1)
        self.assertFalse(summary['success'])

    def test_direct_runner_term_stays_red_and_cleans(self):
        result, summary = self.run_capture(terminate_during_boot=True)
        self.assertEqual(result, 1)
        self.assertFalse(summary['success'])

    def test_supervisor_failure_cleanup_never_qualifies(self):
        for mode in ('checkpoint-failure', 'timeout-live', 'unowned', 'remaining-group'):
            with self.subTest(mode=mode):
                result, summary = self.run_capture(supervisor_mode=mode)
                self.assertEqual(result, 1); self.assertFalse(summary['success'])

    def test_supervisor_group_and_actual_stop_binding(self):
        for field in ('inheritLauncherGroup', 'supervisorPID', 'supervisorPGID', 'supervisorSID', 'launcherPGID',
                      'consoleTransport', 'appAbsenceMethod', 'appAbsentAfterStop'):
            self.check_capture(lambda root, field=field: common.change_json(root, gate.NAME + '.receipt.json',
                lambda receipt: receipt.pop(field)))
        for field, value in [('supervisorGroupAbsentAfterStop', False), ('forcedGroupKill', True),
                             ('supervisorCleanupRequired', True), ('supervisorPID', 7999)]:
            self.check_capture(lambda root, field=field, value=value: common.change_bound_json(root,
                'cleanup.json', 'cleanupSHA256', lambda cleanup: cleanup.update({field: value})))
        self.check_capture(lambda root: common.change_json(root, gate.NAME + '.receipt.json',
            lambda receipt: receipt.update(launcherPGID=7999)))

    def check_rows(self, edit=None):
        rows, control, manifest = sample()
        if edit is None:
            self.assertEqual(gate.qualify(rows, control, manifest, 123)['publicDialRequests'], 2)
        else:
            edit(rows, control, manifest)
            with self.assertRaises((EvidenceError, KeyError, TypeError, StopIteration)):
                gate.qualify(rows, control, manifest, 123)

    def check_capture(self, edit=None):
        with tempfile.TemporaryDirectory(prefix='sdk-m3-direct-controls-') as directory:
            root = Path(directory); fixture(root)
            if edit: edit(root)
            with patch.object(gate.m0, 'source_identity', return_value=common.IDENTITY), \
                 patch.object(gate.m0.subprocess, 'run', side_effect=AssertionError('native command forbidden')):
                if edit is None: self.assertTrue(gate.validate(root)['success'])
                else:
                    with self.assertRaises((EvidenceError, OSError, ValueError, KeyError, TypeError, StopIteration)):
                        gate.validate(root)

    def test_positive_direct_public_roundtrips(self):
        self.check_rows(); self.check_capture()

    def test_both_public_events_required(self):
        self.check_rows(lambda rows, *_: rows.remove(next(r for r in rows if r['event'] == 'm3.dialRequest')))
        self.check_rows(lambda rows, *_: rows.append(copy.deepcopy(next(r for r in rows if r['event'] == 'm3.dialRequest'))))

    def test_actual_url_subscriber_and_typed_intent_required(self):
        for event in ('m3.nativeIngress', 'm3.directIntentEntered'):
            self.check_rows(lambda rows, *_, event=event: rows.remove(next(r for r in rows if r['event'] == event)))
        self.check_rows(lambda rows, *_: next(r for r in rows if r['event'] == 'm3.directDelivery').update(method='private-router'))

    def test_direct_delivery_cannot_be_relabelled_os_dispatch(self):
        self.check_capture(lambda root: common.change_json(root, 'manifest.json', lambda m: m.update(directIngressInjectionRequested=False)))
        self.check_capture(lambda root: common.change_json(root, 'manifest.json', lambda m: m.update(scenario='sdk-m3')))
        self.check_capture(lambda root: common.change_json(root, 'manifest.json', lambda m: m.update(delivery='os-default-dispatch')))

    def test_payload_order_and_zero_conversations(self):
        for key, value in (('video', False), ('conversationCount', 1), ('handleSHA256', '0' * 64)):
            self.check_rows(lambda rows, *_, key=key, value=value: [r for r in rows if r['event'] == 'm3.dialRequest'][1].update({key: value}))
        self.check_rows(lambda rows, *_: next(r for r in rows if r['event'] == 'sdk.snapshot').update(conversations=[{}]))
        self.check_rows(lambda rows, *_: next(r for r in rows if r['event'] == 'sdk.managerCreated').update(owner='ConversationClient.shared'))
        self.check_rows(lambda rows, *_: next(r for r in rows if r['event'] == 'm3.directDeliveryReturned').update(handled=False))

    def test_exact_scalar_protocol(self):
        for key, value in (('pid', 123.0), ('sequence', True), ('t_ms', 0.0), ('wallTimeMS', True)):
            self.check_rows(lambda rows, *_, key=key, value=value: rows[0].update({key: value}))
        for value in (True, 1.0):
            self.check_rows(lambda rows, control, manifest, value=value: control.update(schemaVersion=value))
        self.check_rows(lambda rows, *_: next(r for r in rows if r['event'] == 'm3.dialRequest').update(conversationCount=False))

    def test_listener_control_and_privacy(self):
        self.check_rows(lambda rows, control, manifest: manifest.update(directControlCreatedWallTimeMS=0))
        self.check_rows(lambda rows, *_: next(r for r in rows if r['event'] == 'm3.directControl').update(controlSHA256='0' * 64))
        self.check_rows(lambda rows, *_: rows[0].update(payload='private'))
        self.check_rows(lambda rows, control, manifest: control.update(appPID=456))

    def test_direct_build_and_simulator_entitlement_contract(self):
        with tempfile.TemporaryDirectory(prefix='sdk-m3-direct-build-controls-') as directory:
            app = Path(directory)
            (app / 'Info.plist').write_bytes(plistlib.dumps(dict(CFBundleExecutable='Example')))
            (app / 'Example').write_bytes(macho()); (app / 'main.jsbundle').write_bytes(b'JS')
            identity = common.IDENTITY
            build = dict(schemaVersion=1, product='sdk-m3', configuration='Release', mediaAdapter='none',
                **identity, sourceStart=identity, sourceEnd=identity,
                files={'binaries/example': gate.m0.digest(app / 'Example'),
                       'binaries/main.jsbundle': gate.m0.digest(app / 'main.jsbundle'),
                       'Info.plist': gate.m0.digest(app / 'Info.plist')})
            gate.check_input_build(app, build, identity)
            for edit in [lambda b: b.update(schemaVersion=True), lambda b: b.update(configuration='Debug'),
                         lambda b: b.update(mediaAdapter='webrtc'), lambda b: b.update(sourceEnd={}),
                         lambda b: b['files'].update({'binaries/example': '0' * 64})]:
                changed = copy.deepcopy(build); edit(changed)
                with self.assertRaises(EvidenceError): gate.check_input_build(app, changed, identity)
            for claims in [{}, {'com.apple.developer.calling-app': True, 'com.apple.developer.dialing-app': True}]:
                (app / 'Example').write_bytes(macho(claims))
                changed = copy.deepcopy(build); changed['files']['binaries/example'] = gate.m0.digest(app / 'Example')
                with self.assertRaises(EvidenceError): gate.check_input_build(app, changed, identity)
            for data in [b'', macho()[:31], macho()[:-100]]:
                (app / 'Example').write_bytes(data)
                with self.assertRaises((EvidenceError, ValueError)): gate.simulated_entitlements(app / 'Example')

    def test_direct_build_logs_and_cleanup_are_mandatory(self):
        self.check_capture(lambda root: common.change_bound_json(root, 'input-build-receipt.json',
            'inputBuildReceiptSHA256', lambda b: b.update(buildLogs={})))
        self.check_capture(lambda root: (root / 'build-logs/build.log').write_bytes(b''))
        for field in ('deleted', 'absentFromInventory', 'globalEnvironmentUnchanged'):
            self.check_capture(lambda root, field=field: common.change_bound_json(root, 'cleanup.json',
                'cleanupSHA256', lambda c: c.update({field: False})))

    def test_direct_runner_uncertain_create_cleans_only_its_owned_name(self):
        for options in ({'create_timeout': True}, {'create_invalid': True}):
            result, summary = self.run_capture(**options)
            self.assertEqual(result, 1)
            self.assertFalse(summary['success'])

    def test_source_build_install_and_supervisor_binding(self):
        for name, key in [('input-build-receipt.json', 'inputBuildReceiptSHA256'), ('cleanup.json', 'cleanupSHA256'),
                          ('direct-control.json', 'directControlSHA256')]:
            self.check_capture(lambda root, name=name: (root / name).write_text('{}'))
        self.check_capture(lambda root: (root / (gate.NAME + '.receipt.json')).unlink())
        self.check_capture(lambda root: common.change_bound_json(root, 'commands.json', 'commandsSHA256',
            lambda c: c['steps'][0].update(exitCode=False)))
        self.check_capture(lambda root: common.change_bound_json(root, 'commands.json', 'commandsSHA256',
            lambda c: c['steps'].append(dict(phase='os-tel-dispatch', exitCode=0))))
        def change_info(root):
            value = plistlib.loads((root / 'Info.plist').read_bytes()); value['CFBundleIdentifier'] = 'other'
            (root / 'Info.plist').write_bytes(plistlib.dumps(value))
            common.change_json(root, 'manifest.json', lambda m: m['files'].update({'Info.plist': gate.m0.digest(root / 'Info.plist')}))
        self.check_capture(change_info)


if __name__ == '__main__':
    unittest.main()
