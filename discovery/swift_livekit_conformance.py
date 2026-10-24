#!/usr/bin/env python3
"""Bind real Swift LiveKit engine journeys; synthetic controls are not native proof."""
import argparse
import json
from pathlib import Path
import re
import uuid

from build_swift_livekit_contract import PINS, ROOT, digest, source
from sdk_m0_conformance import bound_rows
from summarize import one, require

BUNDLE = 'dev.iosvoipsdk.swiftlivekit'
CASES = ('healthy', 'stale-audio')
FILES = ('SDKSwiftLiveKit.app/SDKSwiftLiveKit', 'SDKSwiftLiveKit.app/Info.plist',
         'SDKSwiftLiveKit.app/Frameworks/LiveKitWebRTC.framework/LiveKitWebRTC',
         'SDKSwiftLiveKit.app/Frameworks/RustLiveKitUniFFI.framework/RustLiveKitUniFFI',
         'SDKSwiftLiveKit.app/VoIPSDK_CKSimRuntime.bundle/ios-27.0.json', 'Package.resolved',
         'build.log', 'source-before.json', 'source-after.json', 'cksim-notify')
CLASSIFICATION = 'real pinned Swift LiveKit manual/no-device engine; no Room, server, playback or device audio proof'


def validate_build(build):
    info = json.loads((build / 'build-info.json').read_text())
    identity = source()
    require(info.get('schemaVersion') == 1 and info.get('product') == 'sdk-swift-livekit-contract' and
            info.get('configuration') == 'Debug' and identity['sourceDirty'] is False and
            all(info.get(key) == value for key, value in identity.items()), 'build source differs or is uncommitted')
    require(info.get('files') == {name: digest(build / name) for name in FILES} and
            all((build / name).stat().st_size > 0 for name in FILES), 'build files differ or are empty')
    require(json.loads((build / 'source-before.json').read_text()) == identity ==
            json.loads((build / 'source-after.json').read_text()), 'source changed during build')
    pins = json.loads((build / 'Package.resolved').read_text())['pins']
    require(len(pins) == len(PINS) and {row['identity']: row['state']['revision'] for row in pins} ==
            info.get('resolvedPins') == PINS, 'resolved/built dependency pins differ')
    require(re.fullmatch(r'Xcode 27\.1\nBuild version \S+', info.get('xcodeVersion', '')) and
            int(info['sdkVersion'].split('.')[0]) >= 27 and
            re.fullmatch(r'(arm64|x86_64)-apple-ios17\.4-simulator', info.get('target', '')),
            'requires Xcode 27.1 and an SDK 27 simulator build')
    require((build / 'SDKSwiftLiveKit.app/VoIPSDK_CKSimRuntime.bundle/ios-27.0.json').read_bytes() ==
            (ROOT / 'Sources/Engine/Resources/ios-27.0.json').read_bytes(), 'profile bytes differ from source')
    return info


def ordered(rows, values):
    indices = [rows.index(value) for value in values]
    require(indices == sorted(set(indices)), 'causal event order differs')


def engine_state(row, active):
    state = row.get('state')
    require(row.get('observation') == 'weak-manual-AVAudioEngine' and isinstance(state, dict) and
            set(state) == {'automatic', 'manual', 'input', 'output', 'running', 'publicRunning', 'enginePresent', 'engineManual'} and
            all(type(value) is bool for value in state.values()) and
            all(state[key] is value for key, value in dict(automatic=False, manual=True, input=active, output=active, running=active).items()) and
            (not active or state['enginePresent'] and state['engineManual']) and
            state['enginePresent'] is state['engineManual'], 'actual LiveKit manual engine state differs')
    # publicRunning is a typed diagnostic, never a substitute for the actual
    # weak AVAudioEngine observation (the manual and device engines differ).


def contract(rows, native, mode, request=None, response=None):
    owner, incoming, completed = (one(rows, event) for event in ('livekit.owner', 'livekit.incoming', 'livekit.completed'))
    call = incoming.get('conversationID')
    require(isinstance(call, str) and str(uuid.UUID(call)).upper() == call, 'invalid SDK conversation identity')
    require(isinstance(owner.get('identity'), str) and owner['identity'] and
            completed.get('conversationID') == call and completed.get('empty') is True and
            completed.get('authorized') is False and completed.get('demandStopped') is True,
            'owner or terminal demand/conversation cleanup differs')
    prepared = [row for row in rows if row['event'] == 'livekit.prepared']
    require(type(completed.get('preparations')) is int and completed['preparations'] == len(prepared) and prepared,
            'adapter preparation inventory differs')
    require(all(row.get('automatic') is False for row in prepared), 'LiveKit automatic configuration was enabled')
    require(not any(row['event'] == 'contract.error' for row in rows) and
            one(rows, 'contract.result').get('success') is True, 'native fixture failed')
    ordered(rows, [owner, incoming, prepared[0], one(rows, 'livekit.engine', phase='pending-demand'), completed,
                   one(rows, 'contract.result'), one(rows, 'done')])
    engine_state(one(rows, 'livekit.engine', phase='pending-demand'), False)
    reports = [row for row in native if row['event'] == 'report.incoming']
    require(len(reports) == 1 and len([row for row in native if row['event'] == 'provider.created']) == 1,
            'expected one real provider and incoming report')
    native_call = reports[0].get('callUUID')
    require(isinstance(native_call, str) and str(uuid.UUID(native_call)).upper() == native_call and
            one(native, 'report.completed').get('callUUID') == native_call and
            one(native, 'report.completed').get('error') == 0, 'native incoming report did not settle successfully')
    changes = [row for row in native if row['event'] == 'call.changed']
    require(changes and all(row.get('uuid') == native_call for row in changes) and changes[-1].get('ended') is True,
            'native call ledger is not the single ended call')
    require(not any(row['event'] in ('action.timeout', 'audio.failed', 'provider.invalidated', 'runtime.refused')
                    for row in native), 'unexpected platform failure/reset')
    transactions = [row for row in native if row['event'] == 'transaction.delivered']
    settled = [row for row in native if row['event'] == 'action.settled']
    require(all(isinstance(row.get('actionIDs'), list) and len(row['actionIDs']) == 1 for row in transactions) and
            [row.get('actionID') for row in settled] == [row['actionIDs'][0] for row in transactions] and
            len({row.get('actionID') for row in settled}) == len(settled) and
            all(row.get('fulfilled') is True for row in settled), 'actual platform action settlement differs')
    for transaction, settlement in zip(transactions, settled): ordered(native, [transaction, settlement])
    delegates = [row for row in rows if row['event'] == 'livekit.lckDelegate']
    authorization = [row for row in rows if row['event'] == 'livekit.authorization']
    require(authorization and all(type(row.get('active')) is bool for row in authorization) and
            authorization[0]['active'] is False and authorization[-1]['active'] is False,
            'LCK authorization boundaries missing')
    events = [row for row in rows if row['event'] == 'livekit.event' and
              row.get('type') in ('audioActivationMissing', 'audioActivationFailed')]
    audio = [row['event'] for row in native if row['event'] in ('audio.activated', 'audio.deactivated')]
    if mode == 'healthy':
        require(owner.get('activationTimeout') == 10 and owner.get('recoveryTimeout') == 5 and not events and
                not any(row['event'] == 'fault.audioSuppressed' for row in native), 'unexpected healthy watchdog/fault policy')
        phases = [('pending-demand', False), ('joined', True), ('held', False), ('resumed', True), ('ended', False)]
        require([row.get('phase') for row in rows if row['event'] == 'livekit.engine'] == [p for p, _ in phases],
                'healthy actual engine checkpoint inventory differs')
        for phase, active in phases: engine_state(one(rows, 'livekit.engine', phase=phase), active)
        ordered(rows, [one(rows, 'livekit.engine', phase=phase) for phase, _ in phases] + [completed])
        require([row.get('active') for row in delegates] == [True, False, True, False] and
                [row['active'] for row in authorization if row['active']] == [True, True] and
                audio == ['audio.activated', 'audio.deactivated'] * 2 and
                [row.get('kinds') for row in transactions] == [['CXAnswerCallAction'], ['CXSetHeldCallAction'],
                    ['CXSetHeldCallAction'], ['CXEndCallAction']], 'genuine join/hold/resume/end authority differs')
        for delegate, (phase, active) in zip(delegates, phases[1:]):
            checkpoint = one(rows, 'livekit.engine', phase=phase)
            grants = [row for row in authorization if rows.index(row) < rows.index(checkpoint)]
            require(rows.index(delegate) < rows.index(checkpoint) and grants and grants[-1]['active'] is active and
                    rows.index(grants[-1]) < rows.index(delegate), 'engine checkpoint preceded genuine LCK authorization/forwarding')
    else:
        require(mode == 'stale-audio' and owner.get('activationTimeout') == 1 and owner.get('recoveryTimeout') == 1,
                'stale test policy differs (not default watchdog qualification)')
        require(request is not None and response is not None and
                request.get('operation') == 'faults' and request.get('faults') ==
                dict(staleAudio=True, reportCallbackDelayMS=0, invalidateDuringHandoff=False) and
                response.get('success') is True and response.get('result') == dict(configured=True) and
                all(response.get(key) == request.get(key) for key in ('id', 'runID', 'pid', 'bundleID')),
                'missing or mismatched actual stale fault request/ACK')
        configured = one(rows, 'livekit.faultConfigured')
        require(str(uuid.UUID(request['id'])).upper() == request['id'] and configured.get('commandID') == request['id'],
                'fixture did not consume this fault ACK')
        ordered(rows, [owner, one(rows, 'livekit.readyForFault'), configured, prepared[0]])
        require([(row.get('type'), row.get('conversationID')) for row in events] ==
                [('audioActivationMissing', call), ('audioActivationFailed', call)] and len(prepared) == 2,
                'same-call watchdog/recovery inventory differs')
        ordered(rows, [incoming, events[0], prepared[1], events[1], one(rows, 'livekit.engine', phase='stale-ended'), completed])
        require(not delegates and not any(row['active'] for row in authorization) and not audio and
                [row.get('kinds') for row in transactions] == [['CXAnswerCallAction']],
                'stale call gained audio authority or unexpected native actions')
        suppressed = [row for row in native if row['event'] == 'fault.audioSuppressed']
        require(suppressed and all(reports[0]['sequence'] < row['sequence'] < changes[-1]['sequence'] for row in suppressed),
                'actual fault.audioSuppressed missing or outside this call')
        require([row.get('phase') for row in rows if row['event'] == 'livekit.engine'] == ['pending-demand', 'stale-ended'],
                'stale engine checkpoint inventory differs')
        engine_state(one(rows, 'livekit.engine', phase='stale-ended'), False)
    return dict(success=True, conversationID=call, nativeCallUUID=native_call, engineMode='manual-no-device')


def validate(build, capture):
    info = validate_build(build)
    manifest = json.loads((capture / 'manifest.json').read_text())
    require(manifest.get('schemaVersion') == 1 and manifest.get('source') == source() and manifest.get('buildInfoSHA256') == digest(build / 'build-info.json') and
            manifest.get('classification') == CLASSIFICATION and manifest.get('ownsSimulator') is True and
            manifest.get('runtimeInjectionRequested') is False and set(manifest['cases']) == set(CASES),
            'capture/build/source scope differs')
    require(set(manifest['files']) == {str(path.relative_to(capture)) for path in capture.rglob('*') if path.is_file()
            and path not in (capture / 'manifest.json', capture / 'summary.json')} and
            all(digest(capture / name) == value for name, value in manifest['files'].items()), 'capture file binding differs')
    sim = manifest['simulatorID']; require(str(uuid.UUID(sim)).upper() == sim, 'invalid owned simulator')
    require((capture / 'create.stdout').read_text().strip().upper() == sim, 'created simulator differs from manifest')
    require(Path((capture / 'data-container.stdout').read_text().removesuffix('\n')).is_absolute(), 'container lookup is unbound/empty')
    cleanup = json.loads((capture / 'cleanup.json').read_text())
    remaining = json.loads((capture / 'devices-after.json').read_text())['devices']
    require(cleanup.get('simulatorID') == sim and cleanup.get('deleted') is True and cleanup.get('deleteExitCode') == 0 and
            all(row.get('udid') != sim for rows in remaining.values() for row in rows), 'owned simulator cleanup/absence missing')
    for label in ('install', 'microphone', 'data-container', 'boot', 'bootstatus', 'create', 'delete', 'devices-after'):
        require(json.loads((capture / (label + '.status.json')).read_text()).get('exitCode') == 0,
                'missing/failed owned simulator operation: ' + label)
    runtime = json.loads((capture / 'runtime.json').read_text())
    require(runtime['identifier'].startswith('com.apple.CoreSimulator.SimRuntime.iOS-') and
            int(runtime['version'].split('.')[0]) >= 27 and runtime.get('buildversion'), 'execution runtime metadata missing')
    results, processes, runs = {}, set(), set()
    for mode in CASES:
        identity = dict(simulatorID=sim, runID=manifest['cases'][mode]['runID'])
        require(str(uuid.UUID(identity['runID'])) == identity['runID'], 'invalid fresh run ID')
        rows, receipt = bound_rows(capture, mode, identity, BUNDLE)
        require(receipt.get('appEntryObserved') is True and receipt.get('appAbsentAfterStop') is True and
                receipt.get('appAbsenceMethod') == 'signal-zero' and receipt.get('consoleTransport') == 'host-pty' and
                [row['event'] for row in rows[:2]] == ['app.entry', 'launch'], 'current supervisor/owned PID stop evidence missing')
        require(re.findall(r'(?m)^' + re.escape(BUNDLE) + r': (\d+)\r?$', (capture / (mode + '.log')).read_text()) ==
                [str(receipt['appPID'])], 'actual console launcher PID acknowledgement differs')
        supervisor = manifest['cases'][mode]['supervisorPID']
        require(type(supervisor) is int and supervisor > 0 and receipt.get('inheritLauncherGroup') is True and
                all(type(receipt.get(key)) is int and receipt[key] == supervisor for key in
                    ('supervisorPID', 'supervisorPGID', 'supervisorSID', 'launcherPGID')) and
                supervisor != receipt['launcherPID'] != receipt['appPID'] and supervisor != receipt['appPID'] and
                receipt.get('supervisorGroupAbsentAfterStop') is True,
                'unbound private supervisor/console group or missing absence proof')
        require(not (capture / (mode + '-failed-cleanup.json')).exists(), 'failure cleanup cannot qualify a native journey')
        require(receipt['appPID'] == manifest['cases'][mode]['pid'] and receipt['appPID'] not in processes and
                identity['runID'] not in runs, 'cases reused or mismatched process/run')
        processes.add(receipt['appPID']); runs.add(identity['runID'])
        require([row.get('sequence') for row in rows] == list(range(1, len(rows) + 1)) and
                all(type(row.get('wallTimeMS')) is int and row['wallTimeMS'] > 0 and type(row.get('sequence')) is int for row in rows) and
                all(a['wallTimeMS'] <= b['wallTimeMS'] for a, b in zip(rows, rows[1:])), 'probe sequence/time missing')
        launch = one(rows, 'launch')
        require(launch.get('case') == mode and launch.get('mode') == 'sdk-swift-livekit' and launch.get('traceVersion') == 2 and
                launch.get('rendering') == 'manual-no-device' and launch.get('runtimeInjectionPresent') is False and
                launch.get('simulatorOS') == runtime['version'] and
                launch.get('executableSHA256') == info['files']['SDKSwiftLiveKit.app/SDKSwiftLiveKit'], 'launched product/mode differs')
        journal = capture / (mode + '.runtime.jsonl')
        require(receipt.get('journalSHA256') == digest(journal), 'native journal is unbound')
        native = [json.loads(line) for line in journal.read_text().splitlines()]
        require(native and [row.get('sequence') for row in native] == list(range(1, len(native) + 1)) and
                all(row.get('runID') == identity['runID'] and row.get('pid') == receipt['appPID'] and
                    row.get('bundleID') == BUNDLE and row.get('profile') == 'ios-27.0' and
                    type(row.get('t_ms')) is int for row in native) and
                all(a['t_ms'] <= b['t_ms'] for a, b in zip(native, native[1:])), 'mixed native runtime identity/order')
        require(one(native, 'runtime.installed').get('version') == (ROOT / 'VERSION').read_text().strip(), 'runtime version differs')
        session = json.loads((capture / (mode + '.session.json')).read_text())
        expected_session = dict(schemaVersion=1, runID=identity['runID'], pid=receipt['appPID'], bundleID=BUNDLE,
                    profile='ios-27.0', notification='dev.callkitsim.control.' + str(receipt['appPID']))
        require(type(session) is dict and all(type(session.get(key)) is type(value) and session[key] == value
                    for key, value in expected_session.items()), 'runtime session differs')
        fault = [json.loads((capture / name).read_text()) for name in ('fault-request.json', 'fault-response.json')] if mode == 'stale-audio' else [None, None]
        if mode == 'stale-audio':
            require(json.loads((capture / 'fault-notify.status.json').read_text()).get('exitCode') == 0, 'notifier failed')
            require(all(fault[0].get(key) == value for key, value in dict(runID=identity['runID'], pid=receipt['appPID'], bundleID=BUNDLE).items()),
                    'fault belongs to another process/run')
        results[mode] = contract(rows, native, mode, *fault)
    return dict(success=True, classification=CLASSIFICATION, cases=results, source=manifest['source'],
                executionOS=runtime, xcodeVersion=info['xcodeVersion'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path); parser.add_argument('capture', type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.build, args.capture), indent=2))
