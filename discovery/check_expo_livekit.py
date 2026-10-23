#!/usr/bin/env python3
"""Synthetic gate/runner controls only; these do not measure a native engine."""
import hashlib
import json
from pathlib import Path
import plistlib
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, call, patch

import expo_livekit_conformance as gate
import run_expo_livekit as runner
from summarize import EvidenceError

IDENTITY = dict(sourceCommit='a' * 40, sourceInputsSHA256='b' * 64)
CALL, PUSH, NONCE = [str(uuid.UUID(int=n)).upper() for n in (100, 101, 102)]


def write(path, value):
    path.write_text(json.dumps(value) + '\n')


def log(root, name, rows, pid):
    for sequence, row in enumerate(rows, 1): row['sequence'] = sequence
    path = root / (name + '.log')
    path.write_text(gate.BUNDLE + ': ' + str(pid) + '\n' + ''.join('PROBE ' + json.dumps(r) + '\n' for r in rows))
    receipt = root / (name + '.receipt.json')
    value = json.loads(receipt.read_text()) if receipt.exists() else {}
    value['traceSHA256'] = gate.digest(path); write(receipt, value)


def fixture_case(root, case, pid):
    name = 'expo-livekit-' + case
    rows, engine = [], []
    clock, revision = 0, 0

    def add(event, at=None, **fields):
        nonlocal clock
        clock = max(clock + 1, at or 0)
        row = dict(event=event, t_ms=clock, wallTimeMS=1792627200000 + clock, main=True,
                   pid=pid, runID='run-1', simulatorID='sim-1', bundleID=gate.BUNDLE, **fields)
        rows.append(row); return row

    def assertion(label, call=None): add('m2.assertion', label=label, passed=True, conversationID=call)

    def snapshot(status=None, connected=False):
        nonlocal revision
        calls = [] if status is None else [dict(id=CALL, direction='incoming', status=status,
            isMuted=False, isVideo=False, hasConnectedAt=connected)]
        add('sdk.snapshot', revision=revision, conversations=calls); revision += 1

    def media(label, active=False, counts=(0, 0, 0, 0)):
        add('m2.mediaSnapshot', label=label, moduleID='existing-module', deviceID='existing-adm',
            inputAvailable=label == 'initial' or active, outputAvailable=label == 'initial' or active, isEngineRunning=active,
            category='AVAudioSessionCategoryAmbient' if label in ('initial', 'demand') else 'AVAudioSessionCategoryPlayAndRecord',
            mode='AVAudioSessionModeDefault' if label in ('initial', 'demand') else 'AVAudioSessionModeVoiceChat',
            routeCount=2, currentRouteID='output:observed-speaker-uid', currentRouteType='Speaker',
            **dict(zip(('activatedCount', 'deactivatedCount', 'missingCount', 'failedCount'), counts)))
        assertion(label + '-media'); assertion(label + '-same-engine')

    def handler(kind): add('m2.handler', kind=kind, conversationID=CALL, origin='app')

    add('app.entry'); add('launch', mode='sdk-m2', case=case, traceVersion=2)
    add('sdk.activation', active=True); add('sdk.managerCreated', owner='VoIPApplication.start')
    snapshot(); media('initial')
    assertion('inactive-route-rejected'); assertion('routes-observed')
    add('m2.controlReady', staleAudio=case == 'stale-audio')
    add('m2.pushInjected', pushID=PUSH, mustReport=True, label='primary')
    snapshot('ringing'); assertion('push-ringing', CALL); handler('join'); assertion('one-local-audio-track')
    add('m2.localTrack', count=1, enabled=True); demand = add('m2.recordingDemand', accepted=True)
    media('demand'); snapshot('connecting'); assertion('joined', CALL)
    snapshot('connected', True); assertion('backend-connected', CALL)
    if case == 'livekit':
        add('m2.audioEvent', name='audioActivated', count=1); media('active', True, (1, 0, 0, 0)); media('speaker', True, (1, 0, 0, 0))
        handler('hold'); snapshot('held', True); add('m2.audioEvent', name='audioDeactivated', count=1); media('held', False, (1, 1, 0, 0))
        handler('hold'); snapshot('connected', True); add('m2.audioEvent', name='audioActivated', count=2); media('resumed', True, (2, 1, 0, 0))
        handler('end'); snapshot('ended', True); snapshot(); add('m2.audioEvent', name='audioDeactivated', count=2); media('ended', False, (2, 2, 0, 0))
        assertion('ended-empty-with-demand', CALL)
    else:
        add('m2.audioEvent', at=demand['t_ms'] + 10000, name='audioActivationMissing', count=1, conversationID=CALL)
        add('m2.audioEvent', at=demand['t_ms'] + 15000, name='audioActivationFailed', count=1, conversationID=CALL)
        snapshot('ended', True); snapshot(); media('failed', False, (0, 0, 1, 1)); assertion('stale-ended-empty-with-demand', CALL)
    add('m2.localTrackReleased', count=1); add('m2.result', success=True); add('done')
    receipt = dict(schemaVersion=2, success=True, completed=True, launchExitCode=0, lastProbeEvent='done',
        runID='run-1', simulatorID='sim-1', bundleID=gate.BUNDLE, scenario=name, appPID=pid, launcherAppPID=pid,
        launcherPID=9999, terminationMethod='console-launcher-signal', terminationSignal=15, terminationSignalDelivered=True,
        consoleTransport='host-pty', appAbsenceMethod='signal-zero', appAbsentAfterStop=True,
        inheritLauncherGroup=True, supervisorPID=pid + 1000, supervisorPGID=pid + 1000,
        supervisorSID=pid + 1000, launcherPGID=pid + 1000)
    write(root / (name + '.receipt.json'), receipt); log(root, name, rows, pid)

    def native(event, **fields):
        engine.append(dict(event=event, sequence=len(engine) + 1, t_ms=len(engine), runID='run-1', pid=pid,
                           bundleID=gate.BUNDLE, profile='ios-27.0', **fields))

    def changed(connected, held, ended): native('call.changed', uuid=CALL, outgoing=False, connected=connected, held=held, ended=ended)

    native('runtime.installed', version=(gate.ROOT / 'VERSION').read_text().strip())
    native('provider.created', provider='one-provider')
    native('push.received', pushID=PUSH, api='metadata', mustReport=True)
    native('report.incoming', pushID=PUSH, callUUID=CALL); changed(False, False, False)
    native('report.completed', pushID=PUSH, callUUID=CALL, error=0)
    for event in ('push.completed', 'push.callbackReturned'):
        native(event, pushID=PUSH, api='metadata', mustReport=True, reportEntered=True)
    kinds = ['CXAnswerCallAction', 'CXSetHeldCallAction', 'CXSetHeldCallAction', 'CXEndCallAction'] if case == 'livekit' else ['CXAnswerCallAction']
    for i, kind in enumerate(kinds):
        action = str(uuid.UUID(int=i + 1)).upper()
        native('transaction.delivered', provider='one-provider', actionIDs=[action], kinds=[kind])
        native('action.settled', actionID=action, fulfilled=True); changed(True, i == 1, i == 3)
        if case == 'livekit': native('audio.activated' if i in (0, 2) else 'audio.deactivated')
    if case == 'stale-audio':
        native('fault.audioSuppressed'); native('report.ended', callUUID=CALL, reason=1); changed(True, False, True)
    journal = root / (name + '.runtime.jsonl')
    journal.write_text(''.join(json.dumps(r) + '\n' for r in engine))
    receipt = gate.read(root, name + '.receipt.json'); receipt['journalSHA256'] = gate.digest(journal)
    write(root / (name + '.receipt.json'), receipt)
    write(root / (name + '.session.json'), dict(schemaVersion=1, runID='run-1', pid=pid, bundleID=gate.BUNDLE,
          profile='ios-27.0', notification=f'dev.callkitsim.control.{pid}'))
    request = dict(id=NONCE, runID='run-1', pid=pid, bundleID=gate.BUNDLE, operation='faults',
                   faults=dict(staleAudio=case == 'stale-audio', reportCallbackDelayMS=0, invalidateDuringHandoff=False))
    write(root / (name + '.request.json'), request)
    write(root / (name + '.response.json'), dict(id=NONCE, runID='run-1', pid=pid, bundleID=gate.BUNDLE, success=True, result=dict(configured=True)))
    write(root / (name + '.ready.json'), dict(runID='run-1', case=case, pid=pid, staleAudio=case == 'stale-audio'))


def fixture(root):
    build, capture = root / 'build', root / 'capture'; build.mkdir(); capture.mkdir()
    for name in gate.PRODUCT_FILES:
        path = build / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('synthetic ' + name)
    source = (gate.ROOT / 'Sources/Engine/RuntimeEngine.swift').read_text()
    payload = source.split('  private static let embeddedProfileJSON = #"""\n')[1].split('\n"""#\n')[0].encode()
    (build / gate.APP_FILES[0]).write_bytes(b'synthetic executable\0' + payload)
    (build / 'ios-27.0.json').write_bytes((gate.ROOT / 'Sources/Engine/Resources/ios-27.0.json').read_bytes())
    (build / 'example.app/Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier=gate.BUNDLE,
        CFBundleExecutable='VoIPSDKExample', VoIPSDK=dict(mediaAdapter='livekit', simulatorSupport=True),
        UIBackgroundModes=['voip', 'audio'], NSMicrophoneUsageDescription='Local fixture capture.')))
    (build / gate.APP_FILES[-1]).write_bytes(plistlib.dumps(dict(CFBundleIdentifier='io.livekit.LiveKitWebRTC', CFBundleExecutable='LiveKitWebRTC')))
    write(build / 'Podfile.properties.json', {'ios-voip-sdk.mediaAdapter': 'livekit'})
    write(build / 'IosVoipSDK.podspec.json', dict(source_files=['Sources/**/*.{swift,h,m}', 'Expo/apple/**/*.{swift,h}',
        'ExpoMedia/Shared/ExpoMediaSupport.swift', 'ExpoMedia/LiveKit/**/*.{swift,h,m}', 'ExpoMedia/Shared/VSMediaBinding.h',
        'ExpoMedia/Shared/QueuedMediaAdapter.swift'], dependencies={'ExpoModulesCore': [], 'React-Core': [], 'livekit-react-native-webrtc': ['= 150.1.0']}))
    (build / 'Podfile.lock').write_text('PODS:\n  - livekit-react-native (3.1.0):\n  - livekit-react-native-webrtc (150.1.0):\n  - LiveKitWebRTC (150.7871.03)\n')
    write(build / 'build.json', dict(schemaVersion=1, product='expo-livekit', configuration='Release', sourceBefore=IDENTITY,
        sourceAfter=IDENTITY, sdkVersion='27.1', files=gate.product_files(build), versions=gate.VERSIONS))
    write(capture / 'capture.json', dict(schemaVersion=1, source=IDENTITY, runID='run-1', simulatorID='sim-1',
        runtime=dict(identifier='com.apple.CoreSimulator.SimRuntime.iOS-27-1', version='27.1', buildversion='synthetic'),
        installed={k: gate.digest(build / k) for k in gate.APP_FILES}, buildSHA256=gate.digest(build / 'build.json'),
        supervisorPIDs=dict(livekit=1123, **{'stale-audio': 1124}), supervisorGroupsAbsent=dict.fromkeys(gate.CASES, True),
        inheritedExternalDYLD=dict(present=False, sha256=hashlib.sha256(b'').hexdigest())))
    write(capture / 'cleanup.json', dict(simulatorID='sim-1', deleted=True, absent=True, deleteExitCode=0))
    for case, pid in zip(gate.CASES, (123, 124)): fixture_case(capture, case, pid)
    return build, capture


def change_json(path, change):
    value = json.loads(path.read_text()); change(value); write(path, value)


def change_rows(capture, case, change, engine=False):
    name = 'expo-livekit-' + case
    path = capture / (name + ('.runtime.jsonl' if engine else '.log'))
    rows = [json.loads(line if engine else line.split('PROBE ', 1)[1]) for line in path.read_text().splitlines() if engine or 'PROBE ' in line]
    change(rows)
    if engine:
        for index, row in enumerate(rows, 1): row['sequence'] = index
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        change_json(capture / (name + '.receipt.json'), lambda r: r.update(journalSHA256=gate.digest(path)))
    else: log(capture, name, rows, rows[0]['pid'])


class GateControls(unittest.TestCase):
    def reject(self, change):
        with tempfile.TemporaryDirectory(prefix='expo-livekit-synthetic-') as temporary, patch.object(gate, 'source_identity', return_value=IDENTITY):
            build, capture = fixture(Path(temporary)); change(build, capture)
            with self.assertRaises((EvidenceError, OSError)): gate.validate(build, capture)

    def test_synthetic_positive_is_only_a_gate_control(self):
        with tempfile.TemporaryDirectory(prefix='expo-livekit-synthetic-') as temporary, patch.object(gate, 'source_identity', return_value=IDENTITY):
            result = gate.validate(*fixture(Path(temporary)))
            self.assertEqual(set(result['cases']), {'livekit', 'stale-audio'})

    def test_handler_demand_preserves_the_observed_initial_session(self):
        with tempfile.TemporaryDirectory(prefix='expo-livekit-synthetic-') as temporary, patch.object(gate, 'source_identity', return_value=IDENTITY):
            build, capture = fixture(Path(temporary))
            for case in gate.CASES:
                change_rows(capture, case, lambda rows: [r.update(category='AVAudioSessionCategoryPlayback',
                    mode='AVAudioSessionModeMoviePlayback') for r in rows
                    if r['event'] == 'm2.mediaSnapshot' and r['label'] in ('initial', 'demand')])
            self.assertTrue(gate.validate(build, capture)['success'])

    def test_handler_demand_cannot_configure_category_or_mode_early(self):
        for case in gate.CASES:
            for field, value in [('category', 'AVAudioSessionCategoryPlayAndRecord'), ('mode', 'AVAudioSessionModeVoiceChat')]:
                with self.subTest(case=case, field=field):
                    self.reject(lambda b, c: change_rows(c, case, lambda rows: next(r for r in rows
                        if r['event'] == 'm2.mediaSnapshot' and r['label'] == 'demand').update({field: value})))

    def test_fulfilled_action_requires_category_and_mode_even_without_activation(self):
        for case, label in [('livekit', 'active'), ('stale-audio', 'failed')]:
            for field, value in [('category', 'AVAudioSessionCategoryAmbient'), ('mode', 'AVAudioSessionModeDefault')]:
                with self.subTest(case=case, field=field):
                    self.reject(lambda b, c: change_rows(c, case, lambda rows: next(r for r in rows
                        if r['event'] == 'm2.mediaSnapshot' and r['label'] == label).update({field: value})))

    def test_native_running_identity_route_and_scope_are_required(self):
        for event, fields in [('m2.mediaSnapshot', dict(label='active', isEngineRunning=False)),
                              ('m2.mediaSnapshot', dict(label='held', inputAvailable=True)),
                              ('m2.mediaSnapshot', dict(label='resumed', deviceID='replacement-adm')),
                              ('m2.mediaSnapshot', dict(label='speaker', currentRouteID='speaker')),
                              ('m2.mediaSnapshot', dict(label='speaker', currentRouteType='Receiver'))]:
            with self.subTest(fields=fields):
                self.reject(lambda b, c: change_rows(c, 'livekit', lambda rows: next(r for r in rows if r['event'] == event and r.get('label') == fields['label']).update(fields)))
        self.reject(lambda b, c: change_rows(c, 'stale-audio', lambda rows: next(r for r in rows if r['event'] == 'm2.audioEvent').update(conversationID=PUSH)))
        self.reject(lambda b, c: change_rows(c, 'livekit', lambda rows: next(r for r in reversed(rows) if r['event'] == 'sdk.snapshot' and r['conversations'] and r['conversations'][0]['status'] == 'connected')['conversations'][0].update(hasConnectedAt=False)))

    def test_journal_and_push_boundaries_are_independent(self):
        for event, fields in [('call.changed', dict(uuid=PUSH)), ('action.settled', dict(fulfilled=False)),
                              ('push.callbackReturned', dict(reportEntered=False))]:
            with self.subTest(event=event):
                self.reject(lambda b, c: change_rows(c, 'livekit', lambda rows: next(r for r in rows if r['event'] == event).update(fields), engine=True))
        self.reject(lambda b, c: change_rows(c, 'stale-audio', lambda rows: rows.remove(next(r for r in rows if r['event'] == 'fault.audioSuppressed')), engine=True))
        self.reject(lambda b, c: change_rows(c, 'stale-audio', lambda rows: next(r for r in rows if r['event'] == 'report.ended').update(reason=2), engine=True))

    def test_control_ack_nonce_pid_and_fault_are_required(self):
        for suffix, fields in [('.request.json', dict(pid=999)), ('.request.json', dict(faults=dict(staleAudio=False, reportCallbackDelayMS=0, invalidateDuringHandoff=False))),
                               ('.response.json', dict(id=PUSH)), ('.response.json', dict(success=1)), ('.ready.json', dict(staleAudio=False))]:
            with self.subTest(suffix=suffix, fields=fields):
                self.reject(lambda b, c: change_json(c / ('expo-livekit-stale-audio' + suffix), lambda r: r.update(fields)))

    def test_current_supervisor_proof_and_one_pid_header_are_required(self):
        for fields in [dict(consoleTransport='pipe'), dict(appAbsenceMethod='command'), dict(appAbsentAfterStop=False),
                       dict(launcherAppPID=999), dict(schemaVersion=2.0), dict(terminationMethod='command', terminateExitCode=0)]:
            with self.subTest(fields=fields):
                self.reject(lambda b, c: change_json(c / 'expo-livekit-livekit.receipt.json', lambda r: r.update(fields)))
        def duplicate_header(build, capture):
            path = capture / 'expo-livekit-livekit.log'; path.write_text(path.read_text() + gate.BUNDLE + ': 123\n')
            change_json(capture / 'expo-livekit-livekit.receipt.json', lambda r: r.update(traceSHA256=gate.digest(path)))
        self.reject(duplicate_header)

    def test_source_products_install_and_cleanup_are_bound(self):
        self.reject(lambda b, c: (b / gate.APP_FILES[1]).write_text('changed JS'))
        for path, fields in [('capture.json', dict(source={})), ('capture.json', dict(installed={})),
                             ('capture.json', dict(buildSHA256='0' * 64)), ('cleanup.json', dict(absent=False)),
                             ('cleanup.json', dict(deleteExitCode=False))]:
            with self.subTest(path=path, fields=fields):
                self.reject(lambda b, c: change_json(c / path, lambda r: r.update(fields)))
        self.reject(lambda b, c: change_json(b / 'build.json', lambda r: r['versions'].update({'@livekit/react-native-webrtc': '155.0.1'})))
        self.reject(lambda b, c: change_json(c / 'capture.json', lambda r: r['runtime'].update(
            version='27.0', identifier='com.apple.CoreSimulator.SimRuntime.iOS-27-0')))

    def test_supervisor_group_binding_and_absence_are_required(self):
        for fields in [dict(inheritLauncherGroup=False), dict(supervisorPID=123), dict(supervisorPGID=999),
                       dict(supervisorSID=999), dict(launcherPGID=999), dict(supervisorSID=1123.0), dict(launcherPID=123)]:
            with self.subTest(fields=fields):
                self.reject(lambda b, c: change_json(c / 'expo-livekit-livekit.receipt.json', lambda r: r.update(fields)))
        for fields in [dict(supervisorPIDs={'livekit': 1123}), dict(supervisorPIDs=dict.fromkeys(gate.CASES, True)),
                       dict(supervisorGroupsAbsent=dict.fromkeys(gate.CASES, False)), dict(supervisorGroupsAbsent=dict.fromkeys(gate.CASES, 1))]:
            with self.subTest(fields=fields):
                self.reject(lambda b, c: change_json(c / 'capture.json', lambda r: r.update(fields)))


class RunnerControls(unittest.TestCase):
    def setUp(self):
        # Every runner test must intercept group kills, including unexpected paths.
        patcher = patch.object(runner.os, 'killpg')
        self.killpg = patcher.start(); self.addCleanup(patcher.stop)
        for name in ('getpgid', 'getsid'):
            patcher = patch.object(runner.os, name, return_value=4242)
            setattr(self, name, patcher.start()); self.addCleanup(patcher.stop)

    def test_termination_unwinds_create_or_boot_and_cleans_only_exact_owned_name(self):
        owned = str(uuid.UUID(int=103)).upper()
        expected_name = 'expo-livekit-' + NONCE
        for phase in ('create', 'boot'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(prefix='expo-livekit-runner-') as temporary:
                root = Path(temporary); out = root / 'out'; deleted = False
                def sim(args, *unused, **kwargs):
                    nonlocal deleted
                    if args[:2] == ['list', 'runtimes']:
                        return SimpleNamespace(stdout=json.dumps(dict(runtimes=[dict(
                            identifier='com.apple.CoreSimulator.SimRuntime.iOS-27-1', version='27.1',
                            buildversion='synthetic', platform='iOS', isAvailable=True)])).encode())
                    if args[0] == 'create':
                        self.assertEqual(args[1], expected_name)
                        if phase == 'create': runner.handle_termination(signal.SIGTERM, None)
                        return SimpleNamespace(stdout=owned.encode())
                    if args[0] == 'boot':
                        self.assertEqual(args[1], owned)
                        runner.handle_termination(signal.SIGTERM, None)
                    if args[0] in ('shutdown', 'delete'):
                        self.assertEqual(args[1], owned)
                        if args[0] == 'delete': deleted = True
                        return SimpleNamespace(returncode=0)
                    self.assertEqual(args, ['list', 'devices', '-j'])
                    devices = [dict(name=expected_name + '-other', udid='unrelated'),
                               dict(name='previous-' + expected_name, udid='previous')]
                    if not deleted: devices.append(dict(name=expected_name, udid=owned))
                    return SimpleNamespace(stdout=json.dumps(dict(devices={'runtime': devices})).encode())
                old_handler = object()
                with patch.object(runner, 'source_identity', return_value=IDENTITY), patch.object(runner, 'validate_build', return_value={}), \
                     patch.dict(runner.os.environ, {}, clear=True), patch.object(runner.uuid, 'uuid4', return_value=uuid.UUID(NONCE)), \
                     patch.object(runner, 'sim', side_effect=sim) as command, patch.object(runner.signal, 'signal', return_value=old_handler) as handlers:
                    with self.assertRaises(SystemExit) as stopped: runner.run(root / 'build', out)
                self.assertEqual(stopped.exception.code, 143)
                self.assertEqual([c.args[0] for c in command.call_args_list if c.args[0][0] in ('shutdown', 'delete')],
                                 [['shutdown', owned], ['delete', owned]])
                self.assertEqual(handlers.call_args_list, [call(signal.SIGTERM, signal.SIG_IGN),
                                                           call(signal.SIGTERM, signal.SIG_IGN), call(signal.SIGINT, signal.SIG_IGN),
                                                           call(signal.SIGTERM, old_handler), call(signal.SIGINT, old_handler)])
                cleanup = gate.read(out, 'cleanup.json')
                self.assertTrue(cleanup['deleted'] and cleanup['absent'])
                self.assertFalse((out / 'capture.json').exists())
                self.killpg.assert_not_called()

    def test_failed_scenario_reaps_private_group_with_protected_bounded_kill_fallback(self):
        for mode in ('natural', 'timeout-live', 'timeout-exited'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='expo-livekit-runner-') as temporary:
                root = Path(temporary); out = root / 'out'; out.mkdir()
                process = Mock(); process.pid = 4242
                process.poll.side_effect = [None, 0 if mode == 'timeout-exited' else None]
                original = {signal.SIGTERM: runner.handle_termination, signal.SIGINT: signal.default_int_handler}
                registered = original.copy(); order = []; self.killpg.reset_mock()
                self.getpgid.reset_mock(); self.getsid.reset_mock()
                def set_handler(sig, handler):
                    old = registered[sig]; registered[sig] = handler; return old
                def repeated_termination_is_ignored():
                    self.assertEqual(registered, {signal.SIGTERM: signal.SIG_IGN, signal.SIGINT: signal.SIG_IGN})
                def sim(args, *unused, **kwargs):
                    repeated_termination_is_ignored(); order.append('terminate')
                    return SimpleNamespace(returncode=0)
                def wait(timeout):
                    repeated_termination_is_ignored(); order.append(('wait', timeout))
                    if mode != 'natural' and timeout == 25: raise subprocess.TimeoutExpired('synthetic supervisor', timeout)
                    return 0
                def killpg(pgid, sig):
                    repeated_termination_is_ignored(); order.append(('killpg', pgid, sig))
                    if sig == 0: raise ProcessLookupError('synthetic absent owned group')
                process.wait.side_effect = wait; self.killpg.side_effect = killpg
                with patch.object(runner.subprocess, 'Popen', return_value=process) as launch, \
                     patch.object(runner, 'configure', side_effect=RuntimeError('synthetic configuration failure')), \
                     patch.object(runner, 'sim', side_effect=sim) as command, \
                     patch.object(runner.signal, 'signal', side_effect=set_handler) as handlers:
                    with self.assertRaisesRegex(RuntimeError, 'synthetic configuration failure'):
                        runner.scenario(root / 'build', out, root / 'data', 'livekit', 'run-1', 'owned', '', None)
                self.assertIs(launch.call_args.kwargs['start_new_session'], True)
                self.assertIn('--inherit-launcher-group', launch.call_args.args[0])
                command.assert_called_once_with(['terminate', 'owned', gate.BUNDLE], out, timeout=10, check=False)
                expected = [call.poll(), call.wait(timeout=25)]
                if mode != 'natural': expected += [call.poll(), call.wait(timeout=5)]
                self.assertEqual(process.method_calls, expected)
                expected_order = ['terminate', ('wait', 25)]
                if mode == 'timeout-live': expected_order.append(('killpg', 4242, signal.SIGKILL))
                if mode != 'natural': expected_order.append(('wait', 5))
                self.assertEqual(order, expected_order + [('killpg', 4242, 0)])
                self.assertEqual(self.killpg.call_args_list,
                                 ([call(4242, signal.SIGKILL)] if mode == 'timeout-live' else []) + [call(4242, 0)])
                if mode == 'timeout-live':
                    self.getpgid.assert_called_once_with(4242); self.getsid.assert_called_once_with(4242)
                else:
                    self.getpgid.assert_not_called(); self.getsid.assert_not_called()
                self.assertEqual(handlers.call_args_list, [call(signal.SIGTERM, signal.SIG_IGN), call(signal.SIGINT, signal.SIG_IGN),
                                                           call(signal.SIGTERM, original[signal.SIGTERM]), call(signal.SIGINT, original[signal.SIGINT])])
                self.assertEqual(registered, original)

    def test_timeout_refuses_kill_when_private_group_or_session_ownership_changes(self):
        for fault in ('group', 'session'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(prefix='expo-livekit-runner-') as temporary:
                root = Path(temporary); out = root / 'out'; out.mkdir()
                self.killpg.reset_mock(); self.getpgid.reset_mock(); self.getsid.reset_mock()
                self.getpgid.return_value = 7999 if fault == 'group' else 4242
                self.getsid.return_value = 7999 if fault == 'session' else 4242
                process = Mock(); process.pid = 4242; process.poll.return_value = None
                process.wait.side_effect = subprocess.TimeoutExpired('synthetic supervisor', 25)
                with patch.object(runner.subprocess, 'Popen', return_value=process), \
                     patch.object(runner, 'configure', side_effect=RuntimeError('synthetic configuration failure')), \
                     patch.object(runner, 'sim', return_value=SimpleNamespace(returncode=0)), patch.object(runner.signal, 'signal'):
                    with self.assertRaisesRegex(RuntimeError, 'no longer owns its private group'):
                        runner.scenario(root / 'build', out, root / 'data', 'livekit', 'run-1', 'owned', '', None)
                self.killpg.assert_not_called(); self.getpgid.assert_called_once_with(4242)
                if fault == 'session': self.getsid.assert_called_once_with(4242)
                else: self.getsid.assert_not_called()
                self.assertEqual(process.method_calls, [call.poll(), call.wait(timeout=25), call.poll()])

    def test_live_or_unknown_supervisor_group_is_refused(self):
        for observation in (None, PermissionError('synthetic denied group query'), OSError('synthetic unknown group query')):
            with self.subTest(observation=observation):
                self.killpg.reset_mock(); self.killpg.side_effect = observation
                with self.assertRaises((RuntimeError, OSError)): runner.require_group_absent(4242)
                self.killpg.assert_called_once_with(4242, 0)
        self.killpg.reset_mock(); self.killpg.side_effect = ProcessLookupError('synthetic absent')
        runner.require_group_absent(4242); self.killpg.assert_called_once_with(4242, 0)

    def test_cleanup_still_deletes_after_shutdown_timeout_and_refuses_unknown_absence(self):
        for absent in (True, False, None):
            with self.subTest(absent=absent), tempfile.TemporaryDirectory(prefix='expo-livekit-runner-') as temporary:
                def sim(args, *unused, **kwargs):
                    if args[0] == 'shutdown': raise subprocess.TimeoutExpired('synthetic shutdown', 1)
                    if args[0] == 'delete': return SimpleNamespace(returncode=0)
                    if absent is None: raise subprocess.TimeoutExpired('synthetic inventory', 1)
                    return SimpleNamespace(stdout=json.dumps({'devices': {'runtime': [] if absent else [{'udid': 'owned'}]}}).encode())
                with patch.object(runner, 'sim', side_effect=sim) as command:
                    if absent: runner.cleanup(Path(temporary), 'owned')
                    else:
                        with self.assertRaises(RuntimeError): runner.cleanup(Path(temporary), 'owned')
                    self.assertEqual([c.args[0][0] for c in command.call_args_list], ['shutdown', 'delete', 'list'])

    def test_invalid_session_or_ack_never_publishes_readiness(self):
        for fault in ('session-pid', 'ack-pid', 'ack-missing', 'ack-absent'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(prefix='expo-livekit-runner-') as temporary:
                root = Path(temporary); out = root / 'out'; out.mkdir(); data = root / 'data'
                control = data / 'Documents/.callkit-simulator/run-1'
                for path in (control / 'requests', control / 'responses', data / 'tmp'): path.mkdir(parents=True, exist_ok=True)
                session = dict(schemaVersion=1, runID='run-1', pid=42, bundleID=gate.BUNDLE, profile='ios-27.0', notification='dev.callkitsim.control.42')
                if fault == 'session-pid': session.pop('pid')
                write(control.parent / 'session.json', session)
                def sim(*args, **kwargs):
                    request = json.loads(next((control / 'requests').glob('*.json')).read_text())
                    response = dict(id=request['id'], runID='run-1', pid=999 if fault == 'ack-pid' else 42,
                                    bundleID=gate.BUNDLE, success=True, result=dict(configured=True))
                    if fault == 'ack-missing': response.pop('pid')
                    if fault != 'ack-absent': write(control / 'responses' / (request['id'] + '.json'), response)
                def wait(read, process, seconds):
                    result = read()
                    if result is None: raise RuntimeError('synthetic absent ACK')
                    return result
                with patch.object(runner, 'sim', side_effect=sim), patch.object(runner, 'wait_for', side_effect=wait):
                    with self.assertRaises(RuntimeError): runner.configure(out, data, 'case', 'stale-audio', 'run-1', 'owned', Mock(), root / 'notifier')
                self.assertFalse((out / 'case.ready.json').exists())
                self.assertFalse((data / 'tmp/sdk-m2-control-run-1.json').exists())


if __name__ == '__main__':
    unittest.main()
