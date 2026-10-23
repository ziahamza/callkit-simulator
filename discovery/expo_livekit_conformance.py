#!/usr/bin/env python3
"""Qualify the actual Expo LiveKit local-demand journey, not Room/media quality."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import uuid

import sdk_m0_conformance as m0
from sdk_m1_conformance import projected, read_journal
from summarize import EvidenceError, one, push_report_boundaries, require

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = 'dev.iosvoipsdk.example'
CASES = ('livekit', 'stale-audio')
VERSIONS = {'@livekit/react-native': '3.1.0', '@livekit/react-native-webrtc': '150.1.0',
            'livekit-client': '2.23.0', 'react-dom': '19.3.0'}
APP_FILES = ('example.app/VoIPSDKExample', 'example.app/main.jsbundle', 'example.app/Info.plist',
             'example.app/Frameworks/LiveKitWebRTC.framework/LiveKitWebRTC',
             'example.app/Frameworks/LiveKitWebRTC.framework/Info.plist')
PRODUCT_FILES = (*APP_FILES, 'cksim-notify', 'Podfile.lock', 'Podfile.properties.json',
                 'IosVoipSDK.podspec.json', 'ios-27.0.json')
digest = m0.digest
source_identity = m0.source_identity


def read(root, name):
    return json.loads((root / name).read_text())


def exact(value, expected):
    if type(value) is not type(expected): return False
    if isinstance(expected, dict):
        return value.keys() == expected.keys() and all(exact(value[k], v) for k, v in expected.items())
    if isinstance(expected, (list, tuple)):
        return len(value) == len(expected) and all(exact(a, b) for a, b in zip(value, expected))
    return value == expected


def valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)).upper() == value.upper()
    except (ValueError, AttributeError):
        return False


def product_files(build):
    require(all((build / name).is_file() and (build / name).stat().st_size > 0 for name in PRODUCT_FILES),
            'missing or empty Expo LiveKit product')
    return {name: digest(build / name) for name in PRODUCT_FILES}


def validate_build(build, source_root=ROOT, require_source=True):
    info = read(build, 'build.json')
    identity = source_identity(source_root) if require_source else info.get('sourceBefore')
    require(isinstance(identity, dict) and set(identity) == {'sourceCommit', 'sourceInputsSHA256'} and
            re.fullmatch(r'[0-9a-f]{40}', identity.get('sourceCommit', '')) and
            re.fullmatch(r'[0-9a-f]{64}', identity.get('sourceInputsSHA256', '')), 'invalid build source identity')
    require(type(info.get('schemaVersion')) is int and info['schemaVersion'] == 1 and
            info.get('product') == 'expo-livekit' and info.get('configuration') == 'Release' and
            info.get('sourceBefore') == info.get('sourceAfter') == identity and
            info.get('files') == product_files(build) and info.get('versions') == VERSIONS and
            info.get('sdkVersion') == '27.1', 'build source/configuration/products/pins differ')
    native = plistlib.loads((build / 'example.app/Info.plist').read_bytes())
    config = native.get('VoIPSDK', {})
    require(native.get('CFBundleIdentifier') == BUNDLE and native.get('CFBundleExecutable') == 'VoIPSDKExample' and
            config.get('mediaAdapter') == 'livekit' and config.get('simulatorSupport') is True and
            {'voip', 'audio'} <= set(native.get('UIBackgroundModes', [])) and
            isinstance(native.get('NSMicrophoneUsageDescription'), str) and native['NSMicrophoneUsageDescription'].strip(),
            'selected native launch/audio/permission configuration missing')
    framework = plistlib.loads((build / APP_FILES[-1]).read_bytes())
    require(framework.get('CFBundleIdentifier') == 'io.livekit.LiveKitWebRTC' and
            framework.get('CFBundleExecutable') == 'LiveKitWebRTC', 'embedded LiveKit framework differs')
    require(read(build, 'Podfile.properties.json').get('ios-voip-sdk.mediaAdapter') == 'livekit',
            'generated adapter selection differs')
    spec = read(build, 'IosVoipSDK.podspec.json')
    require(spec.get('source_files') == ['Sources/**/*.{swift,h,m}', 'Expo/apple/**/*.{swift,h}',
            'ExpoMedia/Shared/ExpoMediaSupport.swift', 'ExpoMedia/LiveKit/**/*.{swift,h,m}',
            'ExpoMedia/Shared/VSMediaBinding.h', 'ExpoMedia/Shared/QueuedMediaAdapter.swift'] and
            spec.get('dependencies') == {'ExpoModulesCore': [], 'React-Core': [],
                                       'livekit-react-native-webrtc': ['= 150.1.0']},
            'Podspec does not select only the pinned LiveKit branch')
    lock = (build / 'Podfile.lock').read_text()
    for pod, version in (('livekit-react-native', '3.1.0'), ('livekit-react-native-webrtc', '150.1.0'),
                         ('LiveKitWebRTC', '150.7871.03')):
        require(re.search(r'^  - ' + re.escape(pod + ' (' + version + ')') + r'(?::|$)', lock, re.M),
                'selected Pod version differs: ' + pod)
    require(not re.search(r'^  - react-native-webrtc \(', lock, re.M), 'opposite WebRTC pod was linked')
    canonical = source_root / 'Sources/Engine/Resources/ios-27.0.json'
    source = (source_root / 'Sources/Engine/RuntimeEngine.swift').read_text()
    start = '  #if SWIFT_PACKAGE || CKSIM_SDK_SOURCE\n  private static let embeddedProfileJSON = #"""\n'
    end = '\n"""#\n  #endif'
    require(source.count(start) == source.count(end) == 1, 'embedded profile literal differs')
    literal = source.split(start)[1].split(end)[0]
    require(r'\#' not in literal and digest(build / 'ios-27.0.json') == digest(canonical) and
            exact(json.loads(literal), json.loads(canonical.read_bytes())) and
            literal.encode() in (build / APP_FILES[0]).read_bytes(), 'embedded Release profile differs')
    return info


def controls(root, name, case, manifest, receipt, rows):
    pid, run = receipt['appPID'], manifest['runID']
    require(exact(read(root, name + '.session.json'), dict(schemaVersion=1, runID=run, pid=pid,
            bundleID=BUNDLE, profile='ios-27.0', notification=f'dev.callkitsim.control.{pid}')),
            case + ': runtime control session differs')
    request = read(root, name + '.request.json')
    require(valid_uuid(request.get('id')) and exact(request, dict(id=request['id'], runID=run, pid=pid,
            bundleID=BUNDLE, operation='faults', faults=dict(staleAudio=case == 'stale-audio',
            reportCallbackDelayMS=0, invalidateDuringHandoff=False))), case + ': unbound fault request/nonce')
    require(exact(read(root, name + '.response.json'), dict(id=request['id'], runID=run, pid=pid,
            bundleID=BUNDLE, success=True, result=dict(configured=True))), case + ': fault ACK differs')
    require(exact(read(root, name + '.ready.json'), dict(runID=run, case=case, pid=pid,
            staleAudio=case == 'stale-audio')), case + ': fault readiness differs')
    require(one(rows, 'm2.controlReady').get('staleAudio') is (case == 'stale-audio'),
            case + ': native fault readiness differs')


def media_case(rows, engine, case, simulator_os):
    require(case in CASES, 'unknown Expo LiveKit case')
    require([r['event'] for r in rows[:2]] == ['app.entry', 'launch'] and
            [r.get('sequence') for r in rows] == list(range(1, len(rows) + 1)) and
            all(type(r.get('sequence')) is int and type(r.get('wallTimeMS')) is int and r['wallTimeMS'] > 0 for r in rows),
            case + ': invalid process trace')
    require(not any(r.get('error') or r.get('errorType') or r['event'] in ('sdk.error', 'sdk.timeout') for r in rows),
            case + ': native/JS failure')
    launch = one(rows, 'launch')
    require(launch.get('mode') == 'sdk-m2' and launch.get('case') == case and type(launch.get('traceVersion')) is int and
            launch['traceVersion'] == 2 and one(rows, 'sdk.activation').get('active') is True and
            one(rows, 'sdk.managerCreated').get('owner') == 'VoIPApplication.start' and
            one(rows, 'm2.result').get('success') is True, case + ': SDK/result unavailable')
    labels = ['initial', 'demand'] + (['active', 'speaker', 'held', 'resumed', 'ended'] if case == 'livekit' else ['failed'])
    expected = ['initial-media', 'initial-same-engine', 'inactive-route-rejected', 'routes-observed',
                'push-ringing', 'one-local-audio-track', 'demand-media', 'demand-same-engine', 'joined', 'backend-connected']
    for label in labels[2:]: expected.extend([label + '-media', label + '-same-engine'])
    expected.append('ended-empty-with-demand' if case == 'livekit' else 'stale-ended-empty-with-demand')
    assertions = [r for r in rows if r['event'] == 'm2.assertion']
    require([r.get('label') for r in assertions] == expected and all(r.get('passed') is True for r in assertions),
            case + ': assertion inventory/outcome differs')
    call = one(rows, 'm2.assertion', label='push-ringing').get('conversationID')
    require(valid_uuid(call) and all(r.get('conversationID') in (None, call) for r in rows), case + ': call identity differs')
    snapshots = projected(rows, call)
    samples = [r for r in rows if r['event'] == 'm2.mediaSnapshot']
    require([r.get('label') for r in samples] == labels, case + ': media stage inventory differs')
    identities = {(r.get('moduleID'), r.get('deviceID')) for r in samples}
    require(len(identities) == 1 and all(isinstance(v, str) and v for v in next(iter(identities))),
            case + ': existing module/ADM identity changed/missing')
    counters = ('activatedCount', 'deactivatedCount', 'missingCount', 'failedCount')
    names = ('audioActivated', 'audioDeactivated', 'audioActivationMissing', 'audioActivationFailed')
    counts = dict.fromkeys(counters, 0)
    for row in rows:
        if row['event'] == 'm2.audioEvent':
            require(row.get('name') in names, case + ': unknown audio event')
            key = counters[names.index(row['name'])]; counts[key] += 1
            require(type(row.get('count')) is int and row['count'] == counts[key] and 'conversationId' not in row and
                    (row.get('conversationID') == call if row['name'] in names[2:] else 'conversationID' not in row),
                    case + ': native audio event scope/count differs')
        elif row['event'] == 'm2.mediaSnapshot':
            require(all(type(row.get(k)) is int and row[k] == v for k, v in counts.items()),
                    case + ': media sample differs from observed event counters')
    states = {'initial': (False, 0, 0, 0, 0), 'demand': (False, 0, 0, 0, 0), 'active': (True, 1, 0, 0, 0),
              'speaker': (True, 1, 0, 0, 0), 'held': (False, 1, 1, 0, 0), 'resumed': (True, 2, 1, 0, 0),
              'ended': (False, 2, 2, 0, 0), 'failed': (False, 0, 0, 1, 1)}
    for sample in samples:
        active, *expected_counts = states[sample['label']]
        initial = sample['label'] == 'initial'
        expected_session = tuple(samples[0].get(k) for k in ('category', 'mode')) if sample['label'] in ('initial', 'demand') else (
            'AVAudioSessionCategoryPlayAndRecord', 'AVAudioSessionModeVoiceChat')
        require(all(type(sample.get(k)) is bool for k in ('inputAvailable', 'outputAvailable', 'isEngineRunning')) and
                (sample['isEngineRunning'] is False if initial else
                 all(sample[k] is active for k in ('inputAvailable', 'outputAvailable', 'isEngineRunning'))) and
                all(type(sample.get(k)) is int and sample[k] == v for k, v in zip(counters, expected_counts)),
                case + ': native availability/running/count differs at ' + sample['label'])
        require(type(sample.get('routeCount')) is int and sample['routeCount'] > 0 and
                all(sample.get(k) is None or isinstance(sample[k], str) for k in ('currentRouteID', 'currentRouteType')) and
                all(isinstance(sample.get(k), str) and sample[k] for k in ('category', 'mode')) and
                tuple(sample[k] for k in ('category', 'mode')) == expected_session,
                case + ': route/session observation differs')
    if case == 'livekit':
        speaker = one(samples, 'm2.mediaSnapshot', label='speaker')
        require(speaker.get('currentRouteType') == 'Speaker' and isinstance(speaker.get('currentRouteID'), str) and
                speaker['currentRouteID'].startswith('output:') and len(speaker['currentRouteID']) > len('output:'),
                'livekit: actual speaker output UID/type was not observed')
        require(any(c['status'] == 'held' for r in snapshots for c in r['conversations']), 'livekit: held projection absent')
    handlers = [r for r in rows if r['event'] == 'm2.handler']
    kinds = ['join', 'hold', 'hold', 'end'] if case == 'livekit' else ['join']
    require([r.get('kind') for r in handlers] == kinds and all(r.get('origin') == 'app' and r.get('conversationID') == call for r in handlers),
            case + ': handler delivery differs')
    ringing, joined, backend = [one(rows, 'm2.assertion', label=k) for k in ('push-ringing', 'joined', 'backend-connected')]
    index = rows.index
    require(all(c['hasConnectedAt'] is False and c['status'] != 'connected' for r in snapshots if index(r) < index(joined) for c in r['conversations']) and
            any(index(samples[1]) < index(r) < index(joined) and len(r['conversations']) == 1 and
                r['conversations'][0]['status'] == 'connecting' and 'pending' not in r['conversations'][0] and
                r['conversations'][0]['hasConnectedAt'] is False for r in snapshots), case + ': joined action lacks settled connecting projection')
    require(any(index(joined) < index(r) < index(backend) and len(r['conversations']) == 1 and
                r['conversations'][0]['status'] == 'connected' and 'pending' not in r['conversations'][0] and
                r['conversations'][0]['hasConnectedAt'] is True for r in snapshots) and
            all(c['hasConnectedAt'] is True for r in snapshots for c in r['conversations'] if c['status'] == 'connected'),
            case + ': backend connected report/timestamp absent')
    track, demand, release = [one(rows, event) for event in ('m2.localTrack', 'm2.recordingDemand', 'm2.localTrackReleased')]
    require(exact((track.get('count'), release.get('count')), (1, 1)) and track.get('enabled') is True and demand.get('accepted') is True and
            index(samples[0]) < index(one(rows, 'm2.controlReady')) < index(one(rows, 'm2.pushInjected')) < index(ringing) <
            index(handlers[0]) < index(track) < index(demand) < index(samples[1]) < index(joined) < index(backend) < index(samples[2]) and
            any(index(one(rows, 'm2.pushInjected')) < index(r) < index(ringing) and len(r['conversations']) == 1 and
                r['conversations'][0]['status'] == 'ringing' for r in snapshots) and
            index(samples[-1]) < index(assertions[-1]) < index(release) < index(one(rows, 'm2.result')) and
            index(snapshots[-1]) < index(assertions[-1]) and (case != 'livekit' or index(snapshots[-1]) < index(samples[-1])),
            case + ': demand/control/empty-state/release ordering differs')
    require(push_report_boundaries(engine) == 1, case + ': require one actual PushKit delivery')
    push = one(rows, 'm2.pushInjected')
    received, incoming, reported, completed = [one(engine, event) for event in ('push.received', 'report.incoming', 'report.completed', 'push.completed')]
    api = 'metadata' if tuple(map(int, simulator_os.split('.')[:2])) >= (26, 4) else 'legacy'
    require(valid_uuid(push.get('pushID')) and push.get('mustReport') is True and push.get('label') == 'primary' and
            all(r.get('pushID') == push['pushID'] for r in (received, incoming, reported, completed)) and
            received.get('api') == completed.get('api') == api and received.get('mustReport') is True and
            incoming.get('callUUID') == reported.get('callUUID') == call and type(reported.get('error')) is int and reported['error'] == 0,
            case + ': push/report correlation differs')
    delivered = [r for r in engine if r['event'] == 'transaction.delivered']
    action_kinds = ['CXAnswerCallAction', 'CXSetHeldCallAction', 'CXSetHeldCallAction', 'CXEndCallAction'] if case == 'livekit' else ['CXAnswerCallAction']
    require([r.get('kinds') for r in delivered] == [[k] for k in action_kinds] and
            all(isinstance(r.get('actionIDs'), list) and len(r['actionIDs']) == 1 for r in delivered), case + ': action inventory differs')
    actions = [r['actionIDs'][0] for r in delivered]
    settled = [r for r in engine if r['event'] == 'action.settled']
    require(all(valid_uuid(a) for a in actions) and len(set(actions)) == len(actions) and len(settled) == len(actions) and
            {r.get('actionID') for r in settled} == set(actions) and all(r.get('fulfilled') is True for r in settled) and
            all(engine.index(r) < engine.index(one(settled, 'action.settled', actionID=r['actionIDs'][0])) for r in delivered),
            case + ': action fulfillment differs')
    changes = [r for r in engine if r['event'] == 'call.changed']
    require(changes and all(r.get('uuid') == call and r.get('outgoing') is False and
            all(type(r.get(k)) is bool for k in ('connected', 'held', 'ended')) for r in changes) and changes[-1]['ended'] is True,
            case + ': native call identity/end differs')
    native_events = [r for r in rows if r['event'] == 'm2.audioEvent']
    if case == 'livekit':
        require(exact([(r.get('name'), r.get('count')) for r in native_events], [(names[0], 1), (names[1], 1), (names[0], 2), (names[1], 2)]) and
                [r['event'] for r in engine if r['event'] in ('audio.activated', 'audio.deactivated')] ==
                ['audio.activated', 'audio.deactivated', 'audio.activated', 'audio.deactivated'] and
                not any(r['event'] == 'fault.audioSuppressed' for r in engine) and
                any(r['connected'] and not r['ended'] for r in changes) and any(r['held'] and not r['ended'] for r in changes),
                'livekit: real native audio/connected/hold lifecycle differs')
    else:
        require(exact([(r.get('name'), r.get('count')) for r in native_events], [(names[2], 1), (names[3], 1)]) and
                any(r['event'] == 'fault.audioSuppressed' for r in engine) and
                not any(r['event'] in ('audio.activated', 'audio.deactivated') for r in engine), 'stale-audio: absence events/suppression differ')
        missing, failed = native_events
        require(index(demand) < index(missing) < index(failed) and 0 <= missing['t_ms'] - demand['t_ms'] <= 13000 and
                4900 <= failed['t_ms'] - missing['t_ms'] <= 8000 and failed['t_ms'] <= snapshots[-1]['t_ms'] <= failed['t_ms'] + 8000,
                'stale-audio: bounded demand/recovery/termination liveness differs')
        ended = one(engine, 'report.ended')
        require(ended.get('callUUID') == call and type(ended.get('reason')) is int and ended['reason'] == 1,
                'stale-audio: failed end report missing')
    require(not any(r['event'] == 'm2.hookEvent' and r.get('name') == 'providerReset' for r in rows), case + ': provider reset')
    return dict(success=True, existingModuleAndADM=True, localCaptureDemand=True, stages=labels)


def validate(build, capture, source_root=ROOT):
    info = validate_build(build, source_root)
    manifest = read(capture, 'capture.json')
    require(type(manifest.get('schemaVersion')) is int and manifest['schemaVersion'] == 1 and
            manifest.get('source') == info['sourceBefore'] and manifest.get('buildSHA256') == digest(build / 'build.json') and
            manifest.get('installed') == {k: info['files'][k] for k in APP_FILES} and
            all(isinstance(manifest.get(k), str) and manifest[k] for k in ('runID', 'simulatorID')), 'capture source/build/install binding differs')
    cleanup = read(capture, 'cleanup.json')
    require(cleanup.get('simulatorID') == manifest['simulatorID'] and cleanup.get('deleted') is True and
            cleanup.get('absent') is True and type(cleanup.get('deleteExitCode')) is int and cleanup['deleteExitCode'] == 0,
            'owned simulator cleanup incomplete')
    runtime = manifest.get('runtime', {})
    require(all(isinstance(runtime.get(k), str) and runtime[k] for k in ('identifier', 'version', 'buildversion')) and
            runtime['version'] == '27.1' and
            runtime['identifier'] == 'com.apple.CoreSimulator.SimRuntime.iOS-' + runtime['version'].replace('.', '-'),
            'execution runtime identity/version/build missing')
    inherited = manifest.get('inheritedExternalDYLD', {})
    require(type(inherited.get('present')) is bool and isinstance(inherited.get('sha256'), str) and
            re.fullmatch(r'[0-9a-f]{64}', inherited['sha256']) and
            (inherited['present'] or inherited['sha256'] == hashlib.sha256(b'').hexdigest()), 'inherited environment digest differs')
    supervisors = manifest.get('supervisorPIDs', {})
    require(isinstance(supervisors, dict) and set(supervisors) == set(CASES) and
            all(type(pid) is int and pid > 0 for pid in supervisors.values()) and
            exact(manifest.get('supervisorGroupsAbsent'), dict.fromkeys(CASES, True)), 'owned supervisor groups unbound/not absent')
    results = {}
    for case in CASES:
        name = 'expo-livekit-' + case
        rows, receipt = m0.bound_rows(capture, name, manifest, BUNDLE)
        require(type(receipt.get('schemaVersion')) is int and type(receipt.get('appPID')) is int and
                receipt.get('terminationMethod') == 'console-launcher-signal' and
                receipt.get('consoleTransport') == 'host-pty' and receipt.get('appAbsenceMethod') == 'signal-zero' and
                receipt.get('appAbsentAfterStop') is True and type(receipt.get('launcherPID')) is int and receipt['launcherPID'] > 0 and
                type(receipt.get('launcherAppPID')) is int and type(receipt.get('launchExitCode')) is int,
                case + ': missing current supervisor stop/absence proof')
        require(receipt.get('inheritLauncherGroup') is True and
                all(type(receipt.get(k)) is int and receipt[k] == supervisors[case]
                    for k in ('supervisorPID', 'supervisorPGID', 'supervisorSID', 'launcherPGID')) and
                len({supervisors[case], receipt['launcherPID'], receipt['appPID']}) == 3,
                case + ': supervisor/launcher/app process-group binding differs')
        acknowledgements = re.findall(r'^' + re.escape(BUNDLE) + r': (\d+)\r?$', (capture / (name + '.log')).read_text(), re.M)
        require(acknowledgements == [str(receipt['appPID'])], case + ': require one matching launcher PID acknowledgement')
        engine = read_journal(capture, name, manifest, receipt, (source_root / 'VERSION').read_text().strip())
        require(not any(r['event'].startswith('diagnostic.') or r['event'] in ('action.timeout', 'provider.reset', 'audio.failed') for r in engine),
                case + ': runtime error/timeout/reset')
        controls(capture, name, case, manifest, receipt, rows)
        results[case] = media_case(rows, engine, case, runtime['version'])
    return dict(success=True, scope='Expo local-demand/engine-availability; no Room/network-media quality claim',
                source=info['sourceBefore'], runtime=runtime, cases=results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path); parser.add_argument('capture', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(validate(args.build.resolve(), args.capture.resolve()), indent=2))
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        raise SystemExit('Expo LiveKit conformance failed: ' + str(error))
