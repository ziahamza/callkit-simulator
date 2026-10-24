#!/usr/bin/env python3
"""Pure synthetic controls for the two real-engine acceptance gates; no Apple tools."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import run_swift_livekit as runner
import swift_livekit_conformance as gate
from build_swift_livekit_contract import PINS, ROOT, digest, save
from summarize import EvidenceError

SIM = '00000000-0000-0000-0000-000000000001'
CALL = '00000000-0000-0000-0000-000000000002'
CX = '00000000-0000-0000-0000-000000000003'  # Do not conflate framework identities.
COMMAND = '00000000-0000-0000-0000-000000000004'
IDENTITY = dict(sourceCommit='synthetic', sourceInputsSHA256='0' * 64, sourceDirty=False, fixtureInputs={})


def probe(mode):
    rows = []
    def emit(event, **fields): rows.append(dict(event=event, **fields))
    def state(phase, active): emit('livekit.engine', phase=phase,
        observation='weak-manual-AVAudioEngine',
        state=dict(automatic=False, manual=True, input=active, output=active, running=active,
                   publicRunning=False, enginePresent=active, engineManual=active))
    def prepared(): emit('livekit.prepared', automatic=False)
    def delegate(active):
        emit('livekit.authorization', active=active); emit('livekit.lckDelegate', active=active)
    emit('app.entry')
    emit('launch', mode='sdk-swift-livekit', case=mode, traceVersion=2, rendering='manual-no-device',
         simulatorOS='27.0', runtimeInjectionPresent=False)
    emit('livekit.authorization', active=False)
    emit('livekit.owner', identity='synthetic-owner', activationTimeout=1 if mode == 'stale-audio' else 10,
         recoveryTimeout=1 if mode == 'stale-audio' else 5)
    if mode == 'stale-audio':
        emit('livekit.readyForFault'); emit('livekit.faultConfigured', commandID=COMMAND)
    emit('livekit.incoming', conversationID=CALL)
    prepared(); state('pending-demand', False)
    if mode == 'healthy':
        for phase, active in [('joined', True), ('held', False), ('resumed', True), ('ended', False)]:
            delegate(active); state(phase, active)
    else:
        emit('livekit.event', type='audioActivationMissing', conversationID=CALL)
        prepared()
        emit('livekit.event', type='audioActivationFailed', conversationID=CALL)
        state('stale-ended', False)
    emit('livekit.completed', conversationID=CALL, empty=True, authorized=False, demandStopped=True,
         preparations=2 if mode == 'stale-audio' else 1)
    emit('contract.result', success=True); emit('done')
    return rows


def native(mode):
    rows = [dict(event='runtime.installed', version=(ROOT / 'VERSION').read_text().strip()),
            dict(event='provider.created'), dict(event='report.incoming', callUUID=CX),
            dict(event='report.completed', callUUID=CX, error=0), dict(event='call.changed', uuid=CX, ended=False)]
    kinds = ['CXAnswerCallAction'] if mode == 'stale-audio' else [
        'CXAnswerCallAction', 'CXSetHeldCallAction', 'CXSetHeldCallAction', 'CXEndCallAction']
    for index, kind in enumerate(kinds):
        rows += [dict(event='transaction.delivered', kinds=[kind], actionIDs=[str(index)]),
                 dict(event='action.settled', actionID=str(index), fulfilled=True)]
        rows += [dict(event='fault.audioSuppressed')] if mode == 'stale-audio' else [
            dict(event='audio.activated' if index % 2 == 0 else 'audio.deactivated')]
    rows.append(dict(event='call.changed', uuid=CX, ended=True))
    return rows


def materialize(root):
    build, capture = root / 'build', root / 'capture'; build.mkdir(); capture.mkdir()
    for name in gate.FILES:
        path = build / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('synthetic:' + name)
    (build / 'SDKSwiftLiveKit.app/VoIPSDK_CKSimRuntime.bundle/ios-27.0.json').write_bytes(
        (ROOT / 'Sources/Engine/Resources/ios-27.0.json').read_bytes())
    save(build / 'Package.resolved', dict(pins=[dict(identity=name, state=dict(revision=value)) for name, value in PINS.items()]))
    for name in ('source-before.json', 'source-after.json'): save(build / name, IDENTITY)
    save(build / 'build-info.json', dict(schemaVersion=1, product='sdk-swift-livekit-contract', configuration='Debug',
        **IDENTITY, resolvedPins=PINS, target='arm64-apple-ios17.4-simulator', sdkVersion='27.0',
        xcodeVersion='Xcode 27.1\nBuild version 27A9275', files={name: digest(build / name) for name in gate.FILES}))
    cases = {}
    for index, mode in enumerate(gate.CASES):
        pid, run = 100 + index, '00000000-0000-0000-0000-00000000000' + str(5 + index)
        container = '/synthetic/Devices/' + SIM + '/data/Containers/Data/Application/' + CALL
        cases[mode] = dict(pid=pid, runID=run, supervisorPID=500 + index, dataContainer=container)
        rows = probe(mode)
        rows[1].update(executableSHA256=digest(build / 'SDKSwiftLiveKit.app/SDKSwiftLiveKit'), dataContainer=container)
        for sequence, row in enumerate(rows, 1):
            row.update(sequence=sequence, t_ms=sequence, wallTimeMS=sequence + 1000, main=True,
                       pid=pid, runID=run, simulatorID=SIM, bundleID=gate.BUNDLE)
        (capture / (mode + '.log')).write_text(gate.BUNDLE + ': ' + str(pid) + '\n' +
            ''.join('PROBE ' + json.dumps(row) + '\n' for row in rows))
        engine = native(mode)
        for sequence, row in enumerate(engine, 1):
            row.update(sequence=sequence, t_ms=sequence, pid=pid, runID=run, bundleID=gate.BUNDLE, profile='ios-27.0')
        (capture / (mode + '.runtime.jsonl')).write_text(''.join(json.dumps(row) + '\n' for row in engine))
        save(capture / (mode + '.receipt.json'), dict(schemaVersion=2, success=True, completed=True,
            scenario=mode, bundleID=gate.BUNDLE, runID=run, simulatorID=SIM, appPID=pid, launcherPID=300 + index,
            inheritLauncherGroup=True, supervisorPID=500 + index, supervisorPGID=500 + index,
            supervisorSID=500 + index, launcherPGID=500 + index, supervisorGroupAbsentAfterStop=True,
            launcherAppPID=pid, launchExitCode=-15, terminationMethod='console-launcher-signal',
            terminationSignal=15, terminationSignalDelivered=True, lastProbeEvent='done', appEntryObserved=True,
            appAbsentAfterStop=True, appAbsenceMethod='signal-zero', consoleTransport='host-pty',
            traceSHA256=digest(capture / (mode + '.log')), journalSHA256=digest(capture / (mode + '.runtime.jsonl'))))
        save(capture / (mode + '.session.json'), dict(schemaVersion=1, runID=run, pid=pid, bundleID=gate.BUNDLE,
             profile='ios-27.0', notification='dev.callkitsim.control.' + str(pid), controlTransport='directory-watch-v1'))
    (capture / 'create.stdout').write_text(SIM + '\n')
    save(capture / 'runtime.json', dict(identifier='com.apple.CoreSimulator.SimRuntime.iOS-27-0', version='27.0', buildversion='synthetic'))
    save(capture / 'cleanup.json', dict(simulatorID=SIM, deleteExitCode=0, deleted=True))
    save(capture / 'devices-after.json', dict(devices={}))
    for label in ('install', 'microphone', 'boot', 'bootstatus', 'create', 'delete', 'devices-after', 'fault-notify'):
        save(capture / (label + '.status.json'), dict(exitCode=0))
    request = dict(id=COMMAND, **{key: cases['stale-audio'][key] for key in ('pid', 'runID')}, bundleID=gate.BUNDLE, operation='faults',
                   faults=dict(staleAudio=True, reportCallbackDelayMS=0, invalidateDuringHandoff=False))
    save(capture / 'fault-request.json', request)
    save(capture / 'fault-response.json', {key: request[key] for key in ('id', 'runID', 'pid', 'bundleID')} |
         dict(success=True, result=dict(configured=True)))
    save(capture / 'manifest.json', dict(schemaVersion=1, source=IDENTITY, cases=cases, simulatorID=SIM,
        ownsSimulator=True, runtimeInjectionRequested=False, classification=gate.CLASSIFICATION,
        buildInfoSHA256=digest(build / 'build-info.json')))
    rebind(build, capture)
    return build, capture


def rebind(build, capture):
    manifest = json.loads((capture / 'manifest.json').read_text())
    manifest['files'] = {str(path.relative_to(capture)): digest(path) for path in capture.rglob('*') if path.is_file()
                         and path.name != 'manifest.json'}
    save(capture / 'manifest.json', manifest)


def alter_json(path, change):
    value = json.loads(path.read_text()); change(value); save(path, value)


def alter_rows(capture, mode, change, native=False):
    path = capture / (mode + ('.runtime.jsonl' if native else '.log'))
    lines = path.read_text().splitlines()
    rows = [json.loads(line if native else line.split('PROBE ', 1)[1]) for line in lines if native or 'PROBE ' in line]
    change(rows)
    for index, row in enumerate(rows, 1): row.update(sequence=index, t_ms=index)
    path.write_text(('' if native else lines[0] + '\n') + ''.join(('' if native else 'PROBE ') + json.dumps(row) + '\n' for row in rows))
    alter_json(capture / (mode + '.receipt.json'), lambda row: row.update({
        'journalSHA256' if native else 'traceSHA256': digest(path)}))


def controls():
    checks = []
    with tempfile.TemporaryDirectory(prefix='swift-livekit-controls-') as temporary, patch.object(gate, 'source', return_value=IDENTITY):
        root = Path(temporary).resolve()
        def check(label, change=None, accepted=False):
            directory = root / label; directory.mkdir(); build, capture = materialize(directory)
            if change: change(build, capture)
            rebind(build, capture)
            try: gate.validate(build, capture)
            except (EvidenceError, OSError, KeyError, ValueError, TypeError):
                if accepted: raise
            else:
                if not accepted: raise AssertionError('gate accepted ' + label)
            checks.append(label)
        check('two-complete-real-engine-shaped-fixtures', accepted=True)
        def rows(mode, change, native=False): return lambda b, c: alter_rows(c, mode, change, native)
        def event(values, name, **fields): return next(row for row in values if row['event'] == name and all(row.get(k) == v for k, v in fields.items()))
        check('actual-running-public-scalar-true', rows('healthy', lambda r: event(r, 'livekit.engine', phase='joined')['state'].update(publicRunning=True)), accepted=True)
        check('public-true-actual-engine-stopped', rows('healthy', lambda r: event(r, 'livekit.engine', phase='joined')['state'].update(running=False, publicRunning=True)))
        check('missing-actual-engine-identity', rows('healthy', lambda r: event(r, 'livekit.engine', phase='joined')['state'].update(enginePresent=False)))
        check('observed-engine-not-manual', rows('healthy', lambda r: event(r, 'livekit.engine', phase='joined')['state'].update(engineManual=False)))
        check('off-checkpoint-observed-device-engine', rows('healthy', lambda r: event(r, 'livekit.engine', phase='held')['state'].update(enginePresent=True, engineManual=False)))
        check('missing-observation-source', rows('healthy', lambda r: event(r, 'livekit.engine', phase='joined').pop('observation')))
        check('missing-public-scalar-diagnostic', rows('healthy', lambda r: event(r, 'livekit.engine', phase='joined')['state'].pop('publicRunning')))
        check('joined-api-success-engine-off', rows('healthy', lambda r: event(r, 'livekit.engine', phase='joined')['state'].update(running=False)))
        check('held-engine-still-running', rows('healthy', lambda r: event(r, 'livekit.engine', phase='held')['state'].update(running=True)))
        check('resumed-engine-off', rows('healthy', lambda r: event(r, 'livekit.engine', phase='resumed')['state'].update(running=False)))
        check('ended-engine-enabled', rows('healthy', lambda r: event(r, 'livekit.engine', phase='ended')['state'].update(input=True)))
        check('engine-not-manual', rows('healthy', lambda r: event(r, 'livekit.engine', phase='joined')['state'].update(manual=False)))
        check('automatic-session-enabled', rows('healthy', lambda r: event(r, 'livekit.prepared').update(automatic=True)))
        check('typed-engine-bool', rows('healthy', lambda r: event(r, 'livekit.engine', phase='joined')['state'].update(running=1)))
        check('missing-held-checkpoint', rows('healthy', lambda r: r.remove(event(r, 'livekit.engine', phase='held'))))
        check('missing-genuine-LCK-delegate', rows('healthy', lambda r: r.remove(event(r, 'livekit.lckDelegate'))))
        check('missing-native-activation', rows('healthy', lambda r: r.remove(event(r, 'audio.activated')), True))
        check('native-action-not-fulfilled', rows('healthy', lambda r: event(r, 'action.settled').update(fulfilled=False), True))
        check('missing-native-action', rows('healthy', lambda r: r.remove(event(r, 'action.settled')), True))
        check('missing-real-fault-suppression', rows('stale-audio', lambda r: r.remove(event(r, 'fault.audioSuppressed')), True))
        check('stale-engine-running', rows('stale-audio', lambda r: event(r, 'livekit.engine', phase='stale-ended')['state'].update(running=True)))
        check('stale-invented-activation', rows('stale-audio', lambda r: r.insert(-2, dict(event='livekit.lckDelegate', active=True, **{k: r[-3][k] for k in ('pid', 'runID', 'bundleID', 'main', 'simulatorID', 'wallTimeMS')}))))
        check('missing-watchdog-failed', rows('stale-audio', lambda r: r.remove(event(r, 'livekit.event', type='audioActivationFailed'))))
        check('wrong-watchdog-call', rows('stale-audio', lambda r: event(r, 'livekit.event', type='audioActivationMissing').update(conversationID=CX)))
        check('second-recovery', rows('stale-audio', lambda r: r.insert(-3, deepcopy(event(r, 'livekit.prepared')) | dict(wallTimeMS=r[-4]['wallTimeMS']))))
        check('wrong-stale-policy', rows('stale-audio', lambda r: event(r, 'livekit.owner').update(recoveryTimeout=5)))
        check('terminal-not-empty', rows('stale-audio', lambda r: event(r, 'livekit.completed').update(empty=False)))
        check('demand-not-stopped', rows('healthy', lambda r: event(r, 'livekit.completed').update(demandStopped=False)))
        check('native-call-not-ended', rows('stale-audio', lambda r: r[-1].update(ended=False), True))
        check('native-report-failed', rows('healthy', lambda r: event(r, 'report.completed').update(error=2), True))
        def wrong_header(build, capture):
            path = capture / 'healthy.log'
            path.write_text(path.read_text().replace(gate.BUNDLE + ': 100', gate.BUNDLE + ': 999'))
            alter_json(capture / 'healthy.receipt.json', lambda row: row.update(traceSHA256=digest(path)))
        check('wrong-actual-PID-ACK', wrong_header)
        check('wrong-created-simulator', lambda b, c: (c / 'create.stdout').write_text(CALL))
        check('wrong-actual-image', rows('healthy', lambda r: event(r, 'launch').update(executableSHA256='f' * 64)))
        check('injected-runtime', rows('healthy', lambda r: event(r, 'launch').update(runtimeInjectionPresent=True)))
        check('wrong-render-mode', rows('healthy', lambda r: event(r, 'launch').update(rendering='device')))
        for field in ('inheritLauncherGroup', 'supervisorPID', 'supervisorPGID', 'supervisorSID', 'launcherPGID', 'supervisorGroupAbsentAfterStop'):
            check('missing-' + field, lambda b, c, field=field: alter_json(c / 'healthy.receipt.json', lambda r: r.pop(field)))
        check('wrong-private-owner-group', lambda b, c: alter_json(c / 'healthy.receipt.json', lambda r: r.update(launcherPGID=999)))
        check('failure-cleanup-cannot-qualify', lambda b, c: save(c / 'healthy-failed-cleanup.json', dict(forcedGroupKill=True)))
        check('missing-owned-stop', lambda b, c: alter_json(c / 'healthy.receipt.json', lambda r: r.pop('terminationSignalDelivered')))
        check('missing-PID-ACK', lambda b, c: alter_json(c / 'healthy.receipt.json', lambda r: r.pop('launcherAppPID')))
        check('missing-owned-process-absence', lambda b, c: alter_json(c / 'healthy.receipt.json', lambda r: r.pop('appAbsentAfterStop')))
        check('runtime-session-without-additive-transport', lambda b, c:
            alter_json(c / 'healthy.session.json', lambda r: r.pop('controlTransport')), accepted=True)
        for field, expected in dict(schemaVersion=1, runID='00000000-0000-0000-0000-000000000005',
                pid=100, bundleID=gate.BUNDLE, profile='ios-27.0', notification='dev.callkitsim.control.100').items():
            check('missing-runtime-session-' + field, lambda b, c, field=field:
                alter_json(c / 'healthy.session.json', lambda r: r.pop(field)))
            check('wrong-runtime-session-' + field, lambda b, c, field=field, expected=expected:
                alter_json(c / 'healthy.session.json', lambda r: r.update({field: expected + 1 if type(expected) is int else 'other'})))
            check('typed-runtime-session-' + field, lambda b, c, field=field, expected=expected:
                alter_json(c / 'healthy.session.json', lambda r: r.update({field: float(expected) if type(expected) is int else 0})))
        check('wrong-native-run', rows('healthy', lambda r: r[0].update(runID='other'), True))
        check('wrong-fault-ACK', lambda b, c: alter_json(c / 'fault-response.json', lambda r: r.update(id=CALL)))
        check('fault-not-requested', lambda b, c: alter_json(c / 'fault-request.json', lambda r: r['faults'].update(staleAudio=False)))
        check('wrong-fault-process', lambda b, c: alter_json(c / 'fault-request.json', lambda r: r.update(pid=999)))
        check('notifier-failed', lambda b, c: alter_json(c / 'fault-notify.status.json', lambda r: r.update(exitCode=1)))
        check('still-installed-simulator', lambda b, c: alter_json(c / 'devices-after.json', lambda r: r.update(devices=dict(runtime=[dict(udid=SIM)]))))
        check('delete-failed', lambda b, c: alter_json(c / 'cleanup.json', lambda r: r.update(deleteExitCode=1)))
        check('install-failed', lambda b, c: alter_json(c / 'install.status.json', lambda r: r.update(exitCode=1)))
        check('missing-published-container', rows('healthy', lambda r: event(r, 'launch').pop('dataContainer')))
        for field, value in (('runID', CALL), ('pid', 999), ('bundleID', 'other.bundle'), ('simulatorID', CALL)):
            check('wrong-published-' + field, rows('healthy', lambda r, field=field, value=value: event(r, 'launch').update({field: value})))
        for label, path in (('relative', 'relative'), ('wrong-simulator', '/synthetic/' + CALL + '/data/Containers/Data/Application/' + CALL),
                            ('dotdot', '/synthetic/../Devices/' + SIM + '/data/Containers/Data/Application/' + CALL),
                            ('malformed', '/synthetic/' + SIM + '/data/Containers/Data/Application/not-a-uuid')):
            check('published-container-' + label, rows('healthy', lambda r, path=path: event(r, 'launch').update(dataContainer=path)))
        check('unbound-published-container', lambda b, c: alter_json(c / 'manifest.json',
            lambda r: r['cases']['healthy'].update(dataContainer='/other')))
        def changed_installation(build, capture):
            path = '/synthetic/Devices/' + SIM + '/data/Containers/Data/Application/' + COMMAND
            alter_rows(capture, 'stale-audio', lambda r: event(r, 'launch').update(dataContainer=path))
            alter_json(capture / 'manifest.json', lambda r: r['cases']['stale-audio'].update(dataContainer=path))
        check('different-installation-container', changed_installation)
        check('build-wrong-source', lambda b, c: alter_json(b / 'build-info.json', lambda r: r.update(sourceCommit='old')))
        check('framework-tampered', lambda b, c: (b / 'SDKSwiftLiveKit.app/Frameworks/LiveKitWebRTC.framework/LiveKitWebRTC').write_text('tampered'))
        check('missing-real-framework', lambda b, c: (b / 'SDKSwiftLiveKit.app/Frameworks/RustLiveKitUniFFI.framework/RustLiveKitUniFFI').unlink())
        check('wrong-livekit-pin', lambda b, c: alter_json(b / 'build-info.json', lambda r: r['resolvedPins'].update({'client-sdk-swift': 'f' * 40})))
        check('missing-case', lambda b, c: alter_json(c / 'manifest.json', lambda r: r['cases'].pop('stale-audio')))
        # Exercise actual capture_case path discovery before any sandbox I/O.
        import signal, subprocess
        fixed_run = '00000000-0000-0000-0000-000000000005'
        startup = dict(launch_at29=29, launch_at31=31, launch_at33=33,
                       launch_never=None, launch_beyond90=91, launch_early_exit=None)
        for label in ('healthy-published-home', 'healthy-buffered-header', 'stale-published-home', 'wrong-run', 'wrong-pid', 'wrong-bundle',
                      'wrong-simulator', 'wrong-case', 'wrong-mode', 'wrong-image', 'missing-path', 'relative-path',
                      'wrong-device-path', 'dotdot-path', 'malformed-path', 'absent-path', 'symlink-path', 'wrong-installation',
                      'boolean-pid', 'zero-pid', 'wrong-session-pid', 'wrong-session-profile', 'changed-session-run', *startup):
            directory = root / label; directory.mkdir()
            executable = directory / 'SDKSwiftLiveKit.app/SDKSwiftLiveKit'
            executable.parent.mkdir(); executable.write_text('synthetic executable')
            container = directory / 'Devices' / SIM / 'data/Containers/Data/Application' / CALL
            container.mkdir(parents=True)
            mode = 'stale-audio' if label in ('stale-published-home', 'changed-session-run') else 'healthy'
            launch = dict(event='launch', runID=fixed_run, pid=100, bundleID=gate.BUNDLE, simulatorID=SIM,
                          case=mode, mode='sdk-swift-livekit', executableSHA256=digest(executable), dataContainer=str(container))
            for suffix, field, value in (('run', 'runID', CALL), ('pid', 'pid', 999), ('bundle', 'bundleID', 'other.bundle'),
                    ('simulator', 'simulatorID', CALL), ('case', 'case', 'other'), ('mode', 'mode', 'other'), ('image', 'executableSHA256', 'f' * 64)):
                if label == 'wrong-' + suffix: launch[field] = value
            if label == 'boolean-pid': launch['pid'] = True
            if label == 'zero-pid': launch['pid'] = 0
            if label == 'missing-path': launch.pop('dataContainer')
            if label == 'relative-path': launch['dataContainer'] = 'relative'
            if label == 'wrong-device-path': launch['dataContainer'] = str(container).replace(SIM, COMMAND)
            if label == 'dotdot-path': launch['dataContainer'] = str(directory / '..' / container.relative_to(directory))
            if label == 'malformed-path': launch['dataContainer'] = str(container.parent / 'invalid')
            if label == 'absent-path': launch['dataContainer'] = str(container.parent / COMMAND)
            if label == 'symlink-path':
                alias = container.parent / COMMAND; alias.symlink_to(container, target_is_directory=True)
                launch['dataContainer'] = str(alias)
            runtime = container / 'Documents/.callkit-simulator' / fixed_run; runtime.mkdir(parents=True)
            session = dict(runID=fixed_run, pid=100, bundleID=gate.BUNDLE,
                profile='ios-27.0', notification='dev.callkitsim.control.100')
            if label == 'wrong-session-pid': session['pid'] = 999
            if label == 'wrong-session-profile': session['profile'] = 'other'
            save(runtime.parent / 'session.json', session)
            (runtime / 'events.jsonl').write_text('{}\n')
            entry = dict(event='app.entry', runID=fixed_run, pid=100, bundleID=gate.BUNDLE, simulatorID=SIM)
            if label in ('boolean-pid', 'zero-pid'): entry['pid'] = launch['pid']
            header = '' if label == 'healthy-buffered-header' else gate.BUNDLE + ': 100\n'
            trace = directory / (mode + '.log')
            publication = header + 'PROBE ' + json.dumps(entry) + '\nPROBE ' + json.dumps(launch) + \
                '\nPROBE ' + json.dumps(dict(event='livekit.readyForFault')) + '\n'
            delay = startup.get(label, 0); clock = [0.0]
            if delay == 0: trace.write_text(publication)
            def tick(seconds):
                clock[0] = round(clock[0] + seconds, 3)
                if delay is not None and clock[0] >= delay and not trace.exists(): trace.write_text(publication)
            owner = 44001
            class Process:
                pid = owner
                alive = True
                def poll(self): return None if self.alive and not (label == 'launch_early_exit' and clock[0] >= 10) else 0
                def wait(self, timeout): self.alive = False; return 0
            process = Process(); allowed = label in ('healthy-published-home', 'healthy-buffered-header', 'stale-published-home') or \
                (label in startup and delay is not None and delay < 90)
            original_read = Path.read_text; session_reads = [0]
            def read(path, *args, **kwargs):
                assert allowed or (path.name == 'session.json' and (label.startswith('wrong-session-') or label == 'changed-session-run')) or \
                    path.name not in ('session.json', 'events.jsonl'), 'unbound sandbox read'
                if path.name == 'session.json' and label == 'changed-session-run':
                    session_reads[0] += 1
                    if session_reads[0] == 2: return json.dumps(session | dict(runID='stale-run'))
                return original_read(path, *args, **kwargs)
            def notify(out, build, simulator, observed_runtime, session):
                assert allowed and mode == 'stale-audio' and observed_runtime == runtime, 'unvalidated session reached fault transport'
                response = directory / 'synthetic-response.json'; save(response, {})
                return response
            with patch.object(runner.subprocess, 'Popen', return_value=process) as spawn, \
                 patch.object(runner.time, 'monotonic', side_effect=lambda: clock[0]), \
                 patch.object(runner.time, 'sleep', side_effect=tick), \
                 patch.object(runner, 'command', return_value=subprocess.CompletedProcess([], 0, b'', b'')), \
                 patch.object(runner.uuid, 'uuid4', return_value=__import__('uuid').UUID(fixed_run)), \
                 patch.object(runner, 'fault', side_effect=notify) as fault_call, \
                 patch.object(Path, 'read_text', read), patch.object(runner.signal, 'signal'), \
                 patch.object(runner.os, 'killpg', side_effect=ProcessLookupError):
                try: result = runner.capture_case(directory, directory, SIM, '', mode,
                    str(container.parent / COMMAND) if label == 'wrong-installation' else None)
                except (EvidenceError, ValueError, RuntimeError) as error:
                    assert not allowed and (label in startup or not isinstance(error, RuntimeError)), label
                    if label in startup:
                        assert str(error) == ('supervisor exited before fixture checkpoint' if label == 'launch_early_exit'
                                              else 'fixture checkpoint deadline'), label
                else:
                    assert allowed and result['dataContainer'] == str(container), label
                assert fault_call.call_count == (1 if allowed and mode == 'stale-audio' else 0)
            if label in startup:
                assert clock[0] == (delay if allowed else 10 if label == 'launch_early_exit' else 90), label
                command = spawn.call_args.args[0]
                assert command[command.index('--timeout') + 1] == '90'
            assert (directory / (mode + '.runtime.jsonl')).exists() is allowed
            checks.append(label)
        # Drive capture_case itself with an inert process object. Every signal,
        # group lookup, command and spawn is mocked; these controls never create
        # or signal an actual host process.
        for label in ('private-group-normal', 'checkpoint-failure-graceful-reap',
                      'timeout-kills-exact-owned-group', 'unknown-group-never-killed',
                      'owner-exited-no-group-kill', 'remaining-group-refuses'):
            directory = root / label; directory.mkdir()
            receipt = directory / 'healthy.receipt.json'; save(receipt, {})
            executable = directory / 'SDKSwiftLiveKit.app/SDKSwiftLiveKit'
            executable.parent.mkdir(); executable.write_text('synthetic executable')
            container = directory / 'Devices' / SIM / 'data/Containers/Data/Application' / CALL
            container.mkdir(parents=True)
            (directory / 'healthy.log').write_text('PROBE ' + json.dumps(dict(event='app.entry', runID=fixed_run,
                pid=100, bundleID=gate.BUNDLE, simulatorID=SIM)) + '\n')
            owner = 44001
            import signal, subprocess
            handlers = {signal.SIGTERM: 'original-term', signal.SIGINT: 'original-int'}
            previous = handlers.copy(); signal_calls, group_calls, stopped = [], [], []
            class Process:
                pid = owner
                alive = True
                waits = []
                def poll(self): return None if self.alive else 0
                def wait(self, timeout):
                    self.waits.append(timeout)
                    if timeout == 25 and label in ('timeout-kills-exact-owned-group', 'unknown-group-never-killed', 'owner-exited-no-group-kill'):
                        if label == 'owner-exited-no-group-kill': self.alive = False
                        raise subprocess.TimeoutExpired('synthetic-supervisor', timeout)
                    self.alive = False
                    return 0
            process = Process()
            process.waits = []
            def set_signal(sig, handler):
                old = handlers[sig]; handlers[sig] = handler; signal_calls.append((sig, handler)); return old
            def kill_group(group, sig):
                assert group == owner and all(value == signal.SIG_IGN for value in handlers.values())
                group_calls.append((group, sig))
                if sig == 0:
                    if label == 'remaining-group-refuses': return
                    raise ProcessLookupError('synthetic absent group')
                assert sig == signal.SIGKILL and label == 'timeout-kills-exact-owned-group'
                process.alive = False
            def stop_app(out, name, arguments, **options):
                assert all(value == signal.SIG_IGN for value in handlers.values())
                assert arguments == ['xcrun', 'simctl', 'terminate', SIM, gate.BUNDLE]
                assert options == dict(timeout=10, check=False)
                stopped.append(arguments)
            def checkpoint(*args):
                if label not in ('private-group-normal', 'remaining-group-refuses'):
                    raise RuntimeError('synthetic checkpoint failure')
                return dict(event='launch', runID=fixed_run, pid=100, simulatorID=SIM, bundleID=gate.BUNDLE,
                    case='healthy', mode='sdk-swift-livekit', executableSHA256=digest(executable), dataContainer=str(container)) if not hasattr(checkpoint, 'seen') else dict(
                    runID=fixed_run, pid=100, bundleID=gate.BUNDLE, profile='ios-27.0', notification='dev.callkitsim.control.100')
            def checkpoints(*args):
                value = checkpoint(*args); checkpoint.seen = True; return value
            with patch.object(runner.subprocess, 'Popen', return_value=process) as spawn, \
                 patch.object(runner, 'wait', side_effect=checkpoints), \
                 patch.object(runner.uuid, 'uuid4', return_value=__import__('uuid').UUID(fixed_run)), \
                 patch.object(runner, 'command', side_effect=stop_app), \
                 patch.object(runner.signal, 'signal', side_effect=set_signal), \
                 patch.object(runner.os, 'getpgid', return_value=owner + (label == 'unknown-group-never-killed')), \
                 patch.object(runner.os, 'getsid', return_value=owner), \
                 patch.object(runner.os, 'killpg', side_effect=kill_group):
                try: result = runner.capture_case(directory, directory, SIM, '', 'healthy')
                except (EvidenceError, RuntimeError):
                    assert label != 'private-group-normal'
                else:
                    assert label == 'private-group-normal' and result['supervisorPID'] == owner
            assert spawn.call_args.kwargs['start_new_session'] is True
            assert spawn.call_args.args[0].count('--inherit-launcher-group') == 1
            assert handlers == previous and len(signal_calls) == 4
            if label == 'private-group-normal':
                assert not stopped and process.waits == [100]
                assert json.loads(receipt.read_text())['supervisorGroupAbsentAfterStop'] is True
            elif label == 'remaining-group-refuses':
                assert not stopped and 'supervisorGroupAbsentAfterStop' not in json.loads(receipt.read_text())
            else:
                assert len(stopped) == 1 and process.waits[0] == 25
                if label == 'checkpoint-failure-graceful-reap': assert process.waits == [25]
                else: assert process.waits == [25, 5]
            assert [call for call in group_calls if call[1] != 0] == (
                [(owner, signal.SIGKILL)] if label == 'timeout-kills-exact-owned-group' else [])
            checks.append(label)
        # Exercise real ownership orchestration with no prelaunch container IPC.
        # Every OS call is replaced; existing launch and cleanup bounds remain.
        for label in ('owned-run-no-prelaunch-query',
                      'shutdown-timeout-still-deletes', 'explicit-inherited-override',
                      'empty-launchd-value', 'unknown-launchd-status-refused',
                      'launchd-stderr-refused', 'launchd-unset-with-value-refused',
                      'launchd-timeout-stops-and-cleans'):
            query_failure = label in ('unknown-launchd-status-refused', 'launchd-stderr-refused',
                                      'launchd-unset-with-value-refused',
                                      'launchd-timeout-stops-and-cleans')
            shutdown_timeout = label in ('shutdown-timeout-still-deletes', 'launchd-timeout-stops-and-cleans')
            override = label == 'explicit-inherited-override'
            directory = root / label; directory.mkdir(); build, unused = materialize(directory)
            container = directory / 'data'; container.mkdir(); output = directory / 'actual-driver'
            invocations, shared = [], []
            inherited = '/private/fixture-injector.dylib'
            environment = dict(CONTROL_FIXTURE='unchanged')
            if override: environment['SIMCTL_CHILD_DYLD_INSERT_LIBRARIES'] = inherited
            outer_handlers = {signal.SIGTERM: 'outer-term', signal.SIGINT: 'outer-int'}
            original_outer = outer_handlers.copy()
            def outer_mask(sig, handler):
                previous = outer_handlers[sig]; outer_handlers[sig] = handler; return previous
            def fake_run(argv, **options):
                args = list(map(str, argv)); invocations.append((args, options['timeout']))
                if args[2] in ('shutdown', 'delete') or args[2:4] == ['list', 'devices']:
                    assert all(value == signal.SIG_IGN for value in outer_handlers.values())
                if args[2:4] == ['list', 'runtimes']:
                    value = json.dumps(dict(runtimes=[dict(platform='iOS', isAvailable=True,
                        identifier='com.apple.CoreSimulator.SimRuntime.iOS-27-0', version='27.0', buildversion='synthetic')])).encode()
                elif args[2] == 'create': value = (SIM + '\n').encode()
                elif args[2] == 'spawn':
                    assert args[3:] == [SIM, 'launchctl', 'getenv', 'DYLD_INSERT_LIBRARIES']
                    assert options['timeout'] == 180
                    assert options['env'] == {**environment, 'SIMCTL_CHILD_DYLD_INSERT_LIBRARIES': ''}
                    if label == 'launchd-timeout-stops-and-cleans':
                        raise subprocess.TimeoutExpired(args, options['timeout'], output=b'/private/partial-injector.dylib', stderr=b'partial-error')
                    if label == 'unknown-launchd-status-refused': return subprocess.CompletedProcess(args, 2, b'', b'')
                    if label == 'launchd-stderr-refused': return subprocess.CompletedProcess(args, 0, b'', b'unknown-query-error')
                    if label == 'launchd-unset-with-value-refused': return subprocess.CompletedProcess(args, 1, b'/private/unexpected-injector.dylib', b'')
                    value = b'' if label == 'empty-launchd-value' else (inherited + '\n').encode()
                elif args[2] == 'get_app_container': raise AssertionError('redundant prelaunch container IPC')
                elif args[2:4] == ['list', 'devices']: value = b'{"devices":{}}'
                elif args[2] == 'shutdown' and shutdown_timeout:
                    raise subprocess.TimeoutExpired(args, options['timeout'], output=b'partial-cleanup-output', stderr=b'cleanup-error')
                else: value = b''
                return subprocess.CompletedProcess(args, 0, value, b'')
            def fake_case(build, out, simulator, inherited, mode, expected_container):
                assert expected_container == (str(container) if shared else None)
                shared.append(inherited); return dict(runID='synthetic-' + mode, pid=len(shared),
                    supervisorPID=400 + len(shared), dataContainer=str(container))
            with patch.object(runner, 'validate_build'), patch.object(runner, 'source', return_value=IDENTITY), \
                 patch.object(runner, 'validate', return_value=dict(success=True)), \
                 patch.object(runner, 'capture_case', side_effect=fake_case), \
                 patch.object(runner.subprocess, 'run', side_effect=fake_run), \
                 patch.object(runner.signal, 'signal', side_effect=outer_mask), \
                 patch.object(runner, 'redact', wraps=runner.redact) as redaction, \
                 patch.dict(runner.os.environ, environment, clear=True):
                try:
                    with redirect_stdout(io.StringIO()): runner.capture(build, output)
                except (EvidenceError, subprocess.TimeoutExpired) as error:
                    if label == 'launchd-timeout-stops-and-cleans':
                        assert isinstance(error, subprocess.TimeoutExpired) and error.timeout == 180
                    elif not query_failure: raise
                else:
                    assert not query_failure
                assert isinstance(redaction.call_args.args[1], str)
            assert outer_handlers == original_outer
            query = [call for call in invocations if call[0][2] == 'get_app_container']
            if query_failure:
                assert not query and not [call for call in invocations if call[0][2] == 'install']
            else:
                assert not query
            launch_query = [call for call in invocations if call[0][2] == 'spawn']
            assert not launch_query if override else len(launch_query) == 1
            if launch_query: assert (output / 'launch-environment.stdout').read_bytes() == b''
            assert [call[0][2] for call in invocations[-3:]] == ['shutdown', 'delete', 'list']
            assert [call[1] for call in invocations[-3:]] == [60, 60, 60]
            if query_failure: assert not shared
            else:
                expected = '' if label == 'empty-launchd-value' else inherited
                assert len(shared) == 2 and all(value == expected for value in shared)
            if label == 'launchd-timeout-stops-and-cleans':
                assert (output / 'launch-environment.stderr').read_bytes() == b'partial-error'
                assert json.loads((output / 'launch-environment.status.json').read_text())['exitCode'] is None
                assert json.loads((output / 'shutdown.status.json').read_text())['exitCode'] is None
                assert json.loads((output / 'cleanup.json').read_text())['deleted'] is True
            if shutdown_timeout:
                assert (output / 'shutdown.stdout').read_bytes() == b'partial-cleanup-output'
                assert (output / 'shutdown.stderr').read_bytes() == b'cleanup-error'
            checks.append(label)
    return dict(success=True, classification='synthetic acceptance-gate and driver controls only', checks=len(checks), cases=checks)


if __name__ == '__main__': print(json.dumps(controls(), indent=2))
