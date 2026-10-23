#!/usr/bin/env python3
"""A9 simulator direct-delivery acceptance; never qualifies OS default dispatch."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import struct

import sdk_m0_conformance as m0
from summarize import EvidenceError, one, require

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = 'dev.iosvoipsdk.example'
URL = 'tel:+15555550142'
HANDLE = '+15555550142'
NAME = 'sdk-m3-direct'
FILES = ('binaries/example', 'binaries/main.jsbundle', 'Info.plist',
         'simulated-entitlements.plist', 'host-entitlements.plist')
DELIVERY = 'direct-url-and-intent'
STEPS = (
    ('url', 'UIApplicationDelegate.application(open:options:)', URL, HANDLE, False),
    ('intent', 'VoIPSystemEntryPoints.handle(intent:)', '+15555550143', '+15555550143', True),
)


def simulated_entitlements(path):
    """Read Xcode's simulator claim, not restricted host-signature entitlements."""
    data = path.read_bytes()
    require(len(data) >= 32 and struct.unpack_from('<I', data)[0] == 0xFEEDFACF,
            'M3 requires a thin little-endian 64-bit simulator executable')
    count, command_bytes = struct.unpack_from('<II', data, 16)
    end = 32 + command_bytes
    require(end <= len(data), 'truncated Mach-O commands')
    position, claims = 32, []
    for _ in range(count):
        require(position + 8 <= end, 'truncated Mach-O command')
        command, size = struct.unpack_from('<II', data, position)
        require(size >= 8 and position + size <= end, 'invalid Mach-O command bounds')
        if command == 0x19:  # LC_SEGMENT_64
            require(size >= 72, 'truncated Mach-O segment')
            sections = struct.unpack_from('<I', data, position + 64)[0]
            require(72 + sections * 80 <= size, 'truncated Mach-O sections')
            for index in range(sections):
                section = position + 72 + index * 80
                name = data[section:section + 16].rstrip(b'\0')
                segment = data[section + 16:section + 32].rstrip(b'\0')
                if (segment, name) != (b'__TEXT', b'__entitlements'): continue
                length = struct.unpack_from('<Q', data, section + 40)[0]
                offset = struct.unpack_from('<I', data, section + 48)[0]
                require(0 < length <= 1_048_576 and end <= offset <= len(data) - length,
                        'invalid simulator entitlement section bounds')
                claims.append(data[offset:offset + length].rstrip(b'\0'))
        position += size
    require(position == end and len(claims) == 1, 'missing or duplicate simulator entitlement section')
    value = plistlib.loads(claims[0])
    require(isinstance(value, dict), 'simulator entitlements must be a dictionary')
    return claims[0]


def check_input_build(app, receipt, identity=None):
    identity = m0.source_identity() if identity is None else identity
    require(type(receipt.get('schemaVersion')) is int and receipt.get('schemaVersion') == 1 and receipt.get('product') == 'sdk-m3' and
            receipt.get('configuration') == 'Release' and receipt.get('mediaAdapter') == 'none' and
            receipt.get('sourceStart') == receipt.get('sourceEnd') == identity and
            all(receipt.get(key) == value for key, value in identity.items()) and
            receipt.get('files', {}).get('binaries/example') == m0.digest(m0.executable(app)) and
            receipt.get('files', {}).get('binaries/main.jsbundle') == m0.digest(app / 'main.jsbundle') and
            receipt.get('files', {}).get('Info.plist') == m0.digest(app / 'Info.plist'),
            'M3 example does not match committed build-time source and bytes')
    claims = plistlib.loads(simulated_entitlements(m0.executable(app)))
    require(claims.get('com.apple.developer.calling-app') is True and
            'com.apple.developer.dialing-app' not in claims, 'missing normal Xcode simulator calling entitlement')


def qualify(rows, control, manifest, pid):
    require(type(pid) is int and pid > 0 and rows and
            all(type(row.get('pid')) is int and row.get('pid') == pid and row.get('main') is True and
                row.get('runID') == manifest['runID'] and row.get('simulatorID') == manifest['simulatorID'] and
                row.get('bundleID') == BUNDLE for row in rows), 'mixed direct-delivery process/queue identity')
    require(all(type(row.get(key)) is int for row in rows for key in ('sequence', 't_ms', 'wallTimeMS')) and
            [row['sequence'] for row in rows] == list(range(1, len(rows) + 1)) and
            all(a['t_ms'] <= b['t_ms'] and a['wallTimeMS'] <= b['wallTimeMS'] for a, b in zip(rows, rows[1:])),
            'invalid direct-delivery sequence/clock')
    require([row['event'] for row in rows[:2]] == ['app.entry', 'launch'] and
            one(rows, 'launch').get('mode') == 'sdk-m3-direct' and
            type(one(rows, 'launch').get('traceVersion')) is int and one(rows, 'launch')['traceVersion'] == 2,
            'missing explicit direct-delivery launch')
    require(isinstance(control, dict) and set(control) == {'schemaVersion', 'runID', 'simulatorID', 'appPID', 'delivery'} and
            type(control.get('schemaVersion')) is int and control['schemaVersion'] == 1 and
            control.get('runID') == manifest['runID'] and control.get('simulatorID') == manifest['simulatorID'] and
            type(control.get('appPID')) is int and control['appPID'] == pid and control.get('delivery') == DELIVERY,
            'direct control is not bound to this process and mode')
    ready, accepted = one(rows, 'm3.listenerReady'), one(rows, 'm3.directControl')
    created = manifest.get('directControlCreatedWallTimeMS')
    require(type(created) is int and ready['wallTimeMS'] <= created <= accepted['wallTimeMS'] + 2 and
            accepted.get('controlSHA256') == manifest.get('directControlSHA256') and
            rows.index(ready) < rows.index(accepted), 'direct delivery preceded actual public listener/control')
    require(all(type(row.get('conversationCount')) is int and row['conversationCount'] == 0 for row in (ready, accepted)),
            'direct delivery started with a conversation')
    starts = [row for row in rows if row['event'] == 'm3.directDelivery']
    returned = [row for row in rows if row['event'] == 'm3.directDeliveryReturned']
    events = [row for row in rows if row['event'] == 'm3.dialRequest']
    require(len(starts) == len(returned) == len(events) == 2, 'missing or duplicate direct/public delivery')
    ingress, intent = one(rows, 'm3.nativeIngress'), one(rows, 'm3.directIntentEntered')
    require(ingress.get('kind') == 'url' and ingress.get('urlSHA256') == hashlib.sha256(URL.encode()).hexdigest(),
            'URL did not reach the actual application-delegate subscriber')
    require(intent.get('isStartCallIntent') is True, 'missing resolved typed start-call intent')
    last = rows.index(accepted)
    for index, (kind, method, raw, handle, video) in enumerate(STEPS):
        start, result, event = starts[index], returned[index], events[index]
        require(start.get('kind') == result.get('kind') == kind and start.get('method') == method and
                start.get('inputSHA256') == hashlib.sha256(raw.encode()).hexdigest() and result.get('handled') is True,
                'substituted direct entry point or rejected delivery')
        middle = ingress if kind == 'url' else intent
        require(last < rows.index(start) < rows.index(middle) < rows.index(event) and
                rows.index(start) < rows.index(result) < rows.index(events[index + 1] if index == 0 else one(rows, 'sdk.result')),
                'direct entry/public event order differs')
        require(type(event.get('conversationCount')) is int and event['conversationCount'] == 0 and
                event.get('handleSHA256') == hashlib.sha256(handle.encode()).hexdigest() and event.get('video') is video,
                'public direct destination/video differs or routing created a conversation')
        last = max(rows.index(event), rows.index(result))
    snapshots = [row for row in rows if row['event'] == 'sdk.snapshot']
    require(last < rows.index(one(rows, 'sdk.result')) < rows.index(one(rows, 'done')) == len(rows) - 1 and
            one(rows, 'sdk.result').get('success') is True and one(rows, 'sdk.activation').get('active') is True and
            one(rows, 'sdk.managerCreated').get('owner') == 'VoIPApplication.start' and
            snapshots and all(row.get('conversations') == [] for row in snapshots),
            'direct journey failed or created a conversation')
    require(not any(row.get('error') or row.get('errorType') for row in rows) and
            not any(key in row for row in rows for key in ('handle', 'url', 'token', 'value', 'payload', 'caller')),
            'direct journey failed or disclosed private input')
    return dict(success=True, scenario='sdk-m3-direct', delivery=DELIVERY, publicDialRequests=2,
                defaultCallingDispatch='not measured by direct delivery')


def validate(root, source_root=ROOT):
    manifest = json.loads((root / 'manifest.json').read_text())
    identity = m0.source_identity(source_root)
    require(type(manifest.get('schemaVersion')) is int and manifest['schemaVersion'] == 1 and
            manifest.get('scenario') == 'sdk-m3-direct' and manifest.get('delivery') == DELIVERY and
            manifest.get('ownsSimulator') is True and manifest.get('runtimeInjectionRequested') is False and
            manifest.get('directIngressInjectionRequested') is True and
            all(manifest.get(key) == value for key, value in identity.items()), 'invalid explicit direct-delivery source/ownership')
    build = json.loads((root / 'input-build-receipt.json').read_text())
    require(m0.digest(root / 'input-build-receipt.json') == manifest.get('inputBuildReceiptSHA256') and
            type(build.get('schemaVersion')) is int and build['schemaVersion'] == 1 and build.get('product') == 'sdk-m3' and
            build.get('configuration') == 'Release' and build.get('mediaAdapter') == 'none' and
            build.get('sourceStart') == build.get('sourceEnd') == identity and
            all(build.get(key) == value for key, value in identity.items()), 'direct input build is not source-bound')
    require(manifest.get('files') == {name: m0.digest(root / name) for name in FILES} and
            all(build.get('files', {}).get(name) == m0.digest(root / name) for name in
                ('binaries/example', 'binaries/main.jsbundle', 'Info.plist')) and
            manifest.get('installed') == {key: m0.digest(root / name) for key, name in
                [('executableSHA256', 'binaries/example'), ('javascriptSHA256', 'binaries/main.jsbundle'), ('infoPlistSHA256', 'Info.plist')]},
            'direct built/installed bytes differ')
    host = plistlib.loads((root / 'host-entitlements.plist').read_bytes())
    require((root / 'simulated-entitlements.plist').read_bytes() == simulated_entitlements(root / 'binaries/example') and
            isinstance(host, dict) and not {'com.apple.developer.calling-app', 'com.apple.developer.dialing-app'} & host.keys(),
            'simulator claim was substituted or added to the host signature')
    claims = plistlib.loads((root / 'simulated-entitlements.plist').read_bytes())
    info = plistlib.loads((root / 'Info.plist').read_bytes())
    require(info.get('CFBundleIdentifier') == BUNDLE and 'voip' in info.get('UIBackgroundModes', []) and
            claims.get('com.apple.developer.calling-app') is True and 'com.apple.developer.dialing-app' not in claims and
            not any(scheme.lower() == 'tel' for row in info.get('CFBundleURLTypes', []) for scheme in row.get('CFBundleURLSchemes', [])),
            'direct example eligibility/declaration differs')
    require(build.get('buildLogs') and all(Path(name).name == name and (root / 'build-logs' / name).stat().st_size > 0 and
            m0.digest(root / 'build-logs' / name) == digest for name, digest in build['buildLogs'].items()),
            'missing, empty or unbound direct build log')
    cleanup = json.loads((root / 'cleanup.json').read_text())
    require(m0.digest(root / 'cleanup.json') == manifest.get('cleanupSHA256') and
            cleanup.get('simulatorID') == manifest.get('simulatorID') and cleanup.get('deleted') is True and
            cleanup.get('absentFromInventory') is True and cleanup.get('globalEnvironmentUnchanged') is True,
            'direct owned cleanup/environment preservation did not complete')
    require(m0.digest(root / 'commands.json') == manifest.get('commandsSHA256') and
            m0.digest(root / 'environment.json') == manifest.get('environmentSHA256') and
            m0.digest(root / 'direct-control.json') == manifest.get('directControlSHA256'), 'unbound direct control/environment')
    environment = json.loads((root / 'environment.json').read_text())
    require(environment.get('simulatorID') == manifest['simulatorID'] and
            all(isinstance(environment.get(key), str) and environment[key] for key in
                ('simulatorRuntimeID', 'simulatorOS', 'simulatorBuild', 'xcodeVersion')), 'missing direct execution environment')
    commands = json.loads((root / 'commands.json').read_text())['steps']
    for phase in ('verify-built-signature', 'read-host-entitlements', 'install', 'app-data'):
        matches = [row for row in commands if row.get('phase') == phase]
        require(len(matches) == 1 and type(matches[0].get('exitCode')) is int and matches[0]['exitCode'] == 0,
                'direct control failed: ' + phase)
    require(not any(row.get('phase') in ('os-tel-dispatch', 'select-calling-app') for row in commands),
            'OS/Settings execution cannot be relabeled direct delivery')
    rows, receipt = m0.bound_rows(root, NAME, manifest, BUNDLE)
    supervisor = manifest.get('supervisorPID')
    require(type(supervisor) is int and supervisor > 0 and receipt.get('inheritLauncherGroup') is True and
            all(type(receipt.get(key)) is int and receipt[key] == supervisor for key in
                ('supervisorPID', 'supervisorPGID', 'supervisorSID', 'launcherPGID')) and
            supervisor != receipt.get('launcherPID') != receipt['appPID'] and supervisor != receipt['appPID'] and
            receipt.get('consoleTransport') == 'host-pty' and receipt.get('terminationMethod') == 'console-launcher-signal' and
            receipt.get('terminationSignal') == 15 and receipt.get('terminationSignalDelivered') is True and
            receipt.get('appAbsenceMethod') == 'signal-zero' and receipt.get('appAbsentAfterStop') is True and
            type(cleanup.get('supervisorPID')) is int and cleanup['supervisorPID'] == supervisor and
            cleanup.get('supervisorCleanupRequired') is False and
            cleanup.get('forcedGroupKill') is False and cleanup.get('supervisorGroupAbsentAfterStop') is True,
            'unbound private supervisor/console group or missing normal owned cleanup')
    require(re.findall(r'(?m)^' + re.escape(BUNDLE) + r': (\d+)\r?$', (root / (NAME + '.log')).read_text()) ==
            [str(receipt['appPID'])], 'actual console launcher PID acknowledgement differs')
    result = qualify(rows, json.loads((root / 'direct-control.json').read_text()), manifest, receipt['appPID'])
    return dict(**result, source=identity, installed=manifest['installed'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    try:
        result = validate(args.root)
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        result = dict(success=False, error=str(error))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['success'] else 1)
