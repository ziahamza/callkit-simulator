#!/usr/bin/env python3
"""Bind the plain Expo M0 journey and source bootstrap checks to their build."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import uuid

from sdk_bootstrap_conformance import validate as bootstrap_conformance
from summarize import EvidenceError, load, one, require, successful_stop

ROOT = Path(__file__).resolve().parent.parent
CASES = ('healthy', 'profile', 'push-after-install')
SOURCE_PATHS = ('Sources', 'Expo', 'ExpoMedia', 'example', 'Package.swift', 'IosVoipSDK.podspec',
                'expo-module.config.json', 'package.json', 'package-lock.json', 'tsconfig.json', 'VERSION',
                'discovery/build_sdk_bootstrap.sh', 'discovery/build_sdk_device_probe.sh',
                'discovery/probe/sdk_bootstrap.swift', 'discovery/run_sdk_m0.sh',
                'discovery/sdk_m0_conformance.py', 'discovery/check_sdk_m0.py',
                'discovery/run_scenario.py', 'discovery/sdk_bootstrap_conformance.py',
                'discovery/sdk_installed_paths.py', 'discovery/check_sdk_installed_paths.py',
                'discovery/check_console_stop.py',
                'discovery/summarize.py', '.github/workflows/sdk-m0.yml', 'app.plugin.js', 'Tests',
                'discovery/sdk_m1_conformance.py', 'discovery/check_sdk_m1.py',
                'discovery/check_sdk_m1_container_capture.py',
                'discovery/build_sdk_m1_contracts.sh', 'discovery/probe/sdk_m1_contracts.swift',
                'discovery/run_audio_lifecycle_legacy.sh',
                'discovery/sdk_m1_contracts_conformance.py', 'discovery/check_sdk_m1_contracts.py',
                'discovery/check_expo_bridge.py', 'discovery/check_expo_plugin.cjs',
                'discovery/check_expo_media.py', 'discovery/check_expo_media_config.py',
                'discovery/check_platform_shims.py', 'discovery/check_sdk_ownership.py',
                'discovery/check_push_delivery.py', 'discovery/check_push_report_entry.py',
                'discovery/build_push_report_entry.sh', 'discovery/run_push_report_entry.py',
                'discovery/check_push_report_capture.py', 'discovery/probe/push_report_entry.swift',
                'discovery/probe/Info.plist', 'discovery/device_oracles.py',
                'discovery/probe/push_entry_main_stall.h', 'discovery/probe/push_entry_main_stall.m',
                'discovery/push_main_stall_conformance.py', 'discovery/check_push_main_stall.py',
                'discovery/check_lck_provider_policy.py',
                'Tools', 'scripts/build-runtime.sh', 'discovery/run_sdk_system.py',
                'discovery/sdk_system_conformance.py', 'discovery/check_sdk_system.py',
                'discovery/check_inherited_launcher_group.py',
                'discovery/check_cli_controls.py', 'discovery/check_cli_push_launch.py',
                'docs/research/callkit-simulator-probe/device-system-ui/2026-10-20-iphone13-ui10-app.jsonl',
                'discovery/build_sdk_device_release.py', 'discovery/device_release_exclusion.py',
                'discovery/check_device_release.py',
                'discovery/sdk_m2_audio_lifecycle.py', 'discovery/check_sdk_m2_audio_lifecycle.py',
                'docs/research/callkit-simulator-probe/audio-session-lifecycle/2026-10-22-iphone13-ios27/device-trace.jsonl',
                'docs/research/callkit-simulator-probe/audio-session-lifecycle/2026-10-22-iphone13-ios27-noio/device-trace.jsonl',
                'docs/research/callkit-simulator-probe/livecommunicationkit')
TARGETS = ('CKSimRuntimeC', 'CKSimRuntime', 'VoIPPlatform', 'VoIPSDK')
BINARIES = dict(example='binaries/example', bootstrap='binaries/bootstrap',
                device='binaries/device', javascript='binaries/main.jsbundle')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_identity(root=ROOT):
    names = subprocess.check_output(['git', 'ls-files', '-z', '--', *SOURCE_PATHS], cwd=root).decode().split('\0')
    names = sorted(name for name in names if name)
    require(names, 'missing SDK source inputs')
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all', '--', *SOURCE_PATHS], cwd=root)
    require(not dirty, 'SDK source inputs must be committed before acceptance')
    inputs = hashlib.sha256()
    for name in names:
        inputs.update(name.encode() + b'\0' + (root / name).read_bytes() + b'\0')
    return dict(sourceCommit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                sourceInputsSHA256=inputs.hexdigest())


def symbol_names(path):
    result = subprocess.run(['xcrun', 'nm', '-j', str(path)], capture_output=True, text=True, timeout=30)
    require(result.returncode == 0, f'nm failed for {path.name}')
    return sorted(set(line.strip() for line in result.stdout.splitlines() if line.strip()))


def validate_symbols(names):
    hooks = {'_install', '_providerInit', '_providerConcreteInit', '_controllerInit',
             '_registryInit', '_bindHook', '_bindConcrete', '_verifySnapshot', '_Engine'}
    for name in names:
        # SwiftPM can emit this linker bookkeeping from its resource-only module.
        if name.startswith('__swift_FORCE_LOAD_$_CKSimRuntime'):
            continue
        require(not name.startswith('_CKSim') and name not in hooks and not any(token in name for token in
                ('CKSimRuntimeIsActive', 'CKSimRuntimeVersion', 'CKSimActivate', 'CKSimGetEngine',
                 'CKSimPrepareEngine', 'CKSimReleaseEngine', 'CKSimMakeCall', 'CKSimPerform',
                 'CKSimInjectPush', 'CKSimInstallPushHooks', 'CKSimReleasePushHooks', 'CKSimCaptureHook',
                 'CKSimReplaceHook', 'CKSimCurrentPushID', 'CKSimResourceDirectory', 'CKSimEmit',
                 'CKSimulatorEngine', 'CKRuntimePayload', 'CKRuntimeMetadata', 'RuntimeEngine',
                 'RuntimeBootstrap', 'RuntimeStorage', 'RuntimePendingAction', 'ProviderRegistration', 'ObserverRegistration')),
                'device product contains runtime implementation: ' + name)


def record_symbols(root, relative):
    names = symbol_names(root / relative)
    validate_symbols(names)
    path = root / (relative + '.symbols')
    path.write_text('\n'.join(names) + ('\n' if names else ''))
    return digest(path)


def device_objects(spm_device):
    for target in TARGETS:
        directories = (target + '.build', target + '-t.build')
        paths = []
        for path in spm_device.rglob('*.o'):
            parts = path.relative_to(spm_device).parts
            # The outer project can also be VoIPSDK.build: bind the target to its platform directory.
            if any(platform == 'Release-iphoneos' and directory in directories
                   for platform, directory in zip(parts, parts[1:])):
                paths.append(path)
        require(paths, 'missing SwiftPM device objects for ' + target)
        for index, path in enumerate(sorted(paths)):
            yield target, f'device-objects/{target}-{index}.o', path


def executable(bundle):
    return bundle / plistlib.loads((bundle / 'Info.plist').read_bytes())['CFBundleExecutable']


def begin_build(path):
    with path.open('x') as stream:
        json.dump(source_identity(), stream, indent=2)
        stream.write('\n')


def build_inputs(app, bootstrap, device, spm_device):
    inputs = dict(example=executable(app), bootstrap=executable(bootstrap),
                  device=device, javascript=app / 'main.jsbundle')
    files = {BINARIES[key]: digest(value) for key, value in inputs.items()}
    objects = [dict(target=target, path=relative, sha256=digest(source))
               for target, relative, source in device_objects(spm_device)]
    return files, objects


def check_build_receipt(path, app, bootstrap, device, spm_device):
    identity = source_identity()
    build = json.loads(path.read_text())
    files, objects = build_inputs(app, bootstrap, device, spm_device)
    require(build.get('schemaVersion') == 1 and build.get('sourceStart') == identity and
            all(build.get(key) == value for key, value in identity.items()) and
            build.get('files') == files and build.get('swiftPMDeviceObjects') == objects,
            'prebuilt products do not match their build-time source receipt')


def record_build(path, start, app, bootstrap, device, spm_device):
    identity = source_identity()
    require(json.loads(start.read_text()) == identity, 'source changed during native builds')
    files, objects = build_inputs(app, bootstrap, device, spm_device)
    with path.open('x') as stream:
        json.dump(dict(schemaVersion=1, **identity, sourceStart=identity, files=files,
                       swiftPMDeviceObjects=objects), stream, indent=2)
        stream.write('\n')


def record_manifest(root, app, bootstrap, device, spm_device, build_receipt, run_id, simulator_id, external):
    """Bind retained build bytes after the runner has cleaned up its simulator."""
    require(json.loads((root / 'source-before.json').read_text()) == source_identity(),
            'SDK source changed during acceptance')
    check_build_receipt(build_receipt, app, bootstrap, device, spm_device)
    manifest = dict(schemaVersion=1, scenario='sdk-m0', configuration='Release',
                    runID=run_id, simulatorID=simulator_id, ownsSimulator=True,
                    runtimeInjectionRequested=False, **source_identity())
    (root / 'binaries').mkdir()
    shutil.copyfile(build_receipt, root / 'build-receipt.json')
    files = {}
    for key, bundle in (('example', app), ('bootstrap', bootstrap)):
        info = plistlib.loads((bundle / 'Info.plist').read_bytes())
        expected = 'dev.iosvoipsdk.example' if key == 'example' else 'dev.callkitsim.sdkbootstrap'
        require(info.get('CFBundleIdentifier') == expected, 'unexpected ' + key + ' bundle')
        shutil.copyfile(executable(bundle), root / BINARIES[key])
        files[BINARIES[key]] = digest(root / BINARIES[key])
    require((app / 'main.jsbundle').is_file() and (app / 'main.jsbundle').stat().st_size > 0,
            'Release example must contain bundled JavaScript')
    shutil.copyfile(app / 'main.jsbundle', root / BINARIES['javascript'])
    shutil.copyfile(device, root / BINARIES['device'])
    shutil.copyfile(device.with_name(device.name + '.build-info.json'), root / 'device-build.json')
    for key in ('javascript', 'device'):
        files[BINARIES[key]] = digest(root / BINARIES[key])
    manifest['deviceSymbolsSHA256'] = record_symbols(root, BINARIES['device'])
    objects = []
    (root / 'device-objects').mkdir()
    for target, relative, path in device_objects(spm_device):
        shutil.copyfile(path, root / relative)
        objects.append(dict(target=target, path=relative, sha256=digest(root / relative),
                            symbolsSHA256=record_symbols(root, relative)))
    manifest.update(files=files, swiftPMDeviceObjects=objects,
                    environmentSHA256=digest(root / 'environment.json'),
                    sourceBeginSHA256=digest(root / 'source-before.json'),
                    cleanupSHA256=digest(root / 'cleanup.json'),
                    installedSHA256=digest(root / 'installed.json'),
                    runtimeSHA256=digest(root / 'runtime.json'),
                    deviceBuildSHA256=digest(root / 'device-build.json'),
                    buildReceiptSHA256=digest(root / 'build-receipt.json'),
                    installedBinarySHA256=json.loads((root / 'installed.json').read_text()),
                    inheritedExternalDYLD=dict(present=bool(external),
                        sha256=hashlib.sha256(external.encode()).hexdigest()))
    logs = root / 'build-logs'
    manifest['buildLogs'] = {path.name: digest(path) for path in sorted(logs.glob('*')) if path.is_file()}
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


def bound_rows(root, name, manifest, bundle):
    rows = load(root, name)
    receipt = json.loads((root / f'{name}.receipt.json').read_text())
    require(receipt.get('schemaVersion') == 2 and receipt.get('success') is True and
            receipt.get('completed') is True and successful_stop(receipt) and
            receipt.get('lastProbeEvent') == 'done' and receipt.get('launchExitCode') in (0, -15, 143),
            name + ': incomplete supervisor receipt')
    require(receipt.get('scenario') == name and receipt.get('bundleID') == bundle,
            name + ': receipt scenario/bundle mismatch')
    require(receipt.get('traceSHA256') == digest(root / f'{name}.log'), name + ': trace hash mismatch')
    launch = one(rows, 'launch')
    for key in ('runID', 'simulatorID'):
        require(receipt.get(key) == manifest[key] == launch.get(key), name + ': ' + key + ' mismatch')
    pid = launch.get('pid')
    require(type(pid) is int and pid > 0 and receipt.get('appPID') == receipt.get('launcherAppPID') == pid,
            name + ': process mismatch')
    require(all(row.get('runID') == manifest['runID'] and row.get('pid') == pid and
                row.get('bundleID') == bundle and row.get('main') is True and
                ('simulatorID' not in row or row['simulatorID'] == manifest['simulatorID']) for row in rows),
            name + ': mixed process or off-main evidence')
    require(rows[-1] is one(rows, 'done'), name + ': missing terminal done')
    require(all(a['t_ms'] <= b['t_ms'] for a, b in zip(rows, rows[1:])), name + ': non-monotonic time')
    return rows, receipt


def validate(root, source_root=ROOT):
    manifest = json.loads((root / 'manifest.json').read_text())
    require(manifest.get('schemaVersion') == 1 and manifest.get('scenario') == 'sdk-m0' and
            manifest.get('configuration') == 'Release' and manifest.get('ownsSimulator') is True and
            manifest.get('runtimeInjectionRequested') is False, 'invalid M0 manifest or injected run')
    identity = source_identity(source_root)
    require(all(manifest.get(key) == value for key, value in identity.items()), 'source identity mismatch')
    require(digest(root / 'source-before.json') == manifest['sourceBeginSHA256'] and
            json.loads((root / 'source-before.json').read_text()) == identity, 'source start/end mismatch')
    require(digest(root / 'cleanup.json') == manifest['cleanupSHA256'], 'cleanup receipt hash mismatch')
    cleanup = json.loads((root / 'cleanup.json').read_text())
    require(cleanup.get('simulatorID') == manifest.get('simulatorID') and cleanup.get('deleted') is True and
            cleanup.get('deleteExitCode') == 0, 'owned simulator cleanup incomplete')
    require(digest(root / 'environment.json') == manifest.get('environmentSHA256'), 'environment hash mismatch')
    environment = json.loads((root / 'environment.json').read_text())
    require(digest(root / 'runtime.json') == manifest['runtimeSHA256'], 'runtime environment hash mismatch')
    runtime = json.loads((root / 'runtime.json').read_text())
    require(environment.get('simulatorID') == manifest.get('simulatorID') and
            environment.get('simulatorRuntimeID') == runtime.get('identifier') and
            environment.get('simulatorOS') == runtime.get('version') and
            all(isinstance(environment.get(key), str) and environment[key] for key in
                ('simulatorRuntimeID', 'simulatorOS', 'xcodeVersion', 'sdkVersion', 'hostArchitecture')),
            'missing or unbound execution environment')
    external = manifest['inheritedExternalDYLD']
    require(type(external.get('present')) is bool and isinstance(external.get('sha256'), str) and
            len(external['sha256']) == 64 and all(c in '0123456789abcdef' for c in external['sha256']),
            'missing inherited environment digest')
    if not external['present']:
        require(external['sha256'] == hashlib.sha256(b'').hexdigest(), 'empty inherited environment mismatch')
    require(set(manifest['files']) == set(BINARIES.values()), 'unexpected binary inventory')
    for path, expected in manifest['files'].items():
        require((root / path).stat().st_size > 0 and digest(root / path) == expected, 'build bytes mismatch: ' + path)
    require(digest(root / 'build-receipt.json') == manifest['buildReceiptSHA256'], 'build receipt hash mismatch')
    build = json.loads((root / 'build-receipt.json').read_text())
    require(build.get('schemaVersion') == 1 and build.get('sourceStart') == identity and
            all(build.get(key) == value for key, value in identity.items()) and
            build.get('files') == manifest['files'], 'build-time source or product identity mismatch')
    require(digest(root / 'device-build.json') == manifest['deviceBuildSHA256'], 'device build receipt hash mismatch')
    device_build = json.loads((root / 'device-build.json').read_text())
    require(device_build.get('product') == 'unified-shared-source-device' and
            device_build.get('target') == 'arm64-apple-ios17.4' and
            device_build.get('binarySHA256') == manifest['files'][BINARIES['device']] and
            all(device_build.get(key) == value for key, value in identity.items()), 'device build/source mismatch')
    for key in ('example', 'bootstrap', 'javascript'):
        require(manifest['installedBinarySHA256'].get(key) == manifest['files'][BINARIES[key]],
                'installed bytes differ from build: ' + key)
    require(digest(root / 'installed.json') == manifest['installedSHA256'] and
            json.loads((root / 'installed.json').read_text()) == manifest['installedBinarySHA256'],
            'installed image receipt mismatch')
    device_names = symbol_names(root / BINARIES['device'])
    require(device_names, 'empty device symbol inventory')
    validate_symbols(device_names)
    check_symbols(root, BINARIES['device'], manifest['deviceSymbolsSHA256'], device_names)
    objects = manifest['swiftPMDeviceObjects']
    require(objects and {obj.get('target') for obj in objects} == set(TARGETS), 'missing SwiftPM device target')
    require(len({obj['path'] for obj in objects}) == len(objects), 'duplicate SwiftPM object')
    require(build.get('swiftPMDeviceObjects') == [{key: obj[key] for key in ('target', 'path', 'sha256')}
            for obj in objects], 'SwiftPM objects differ from native build receipt')
    for obj in objects:
        relative = obj['path']
        require(relative.startswith('device-objects/') and len(Path(relative).parts) == 2 and
                digest(root / relative) == obj['sha256'], 'device object bytes mismatch')
        names = symbol_names(root / relative)
        validate_symbols(names)
        check_symbols(root, relative, obj['symbolsSHA256'], names)
    require(manifest['buildLogs'], 'missing build logs')
    for name, expected in manifest['buildLogs'].items():
        path = root / 'build-logs' / name
        require(Path(name).name == name and path.stat().st_size > 0 and digest(path) == expected, 'build log mismatch')
    require({path.name for path in root.glob('*.receipt.json')} ==
            {'sdk-m0.receipt.json', *(f'bootstrap-{case}.receipt.json' for case in CASES)},
            'missing or unexpected scenario receipts')
    rows, receipt = bound_rows(root, 'sdk-m0', manifest, 'dev.iosvoipsdk.example')
    require(receipt.get('appEntryObserved') is True and [row['event'] for row in rows[:2]] == ['app.entry', 'launch'],
            'missing app startup evidence')
    require(one(rows, 'launch').get('mode') == 'sdk-m0' and one(rows, 'launch').get('traceVersion') == 2,
            'wrong example mode')
    require(all(type(row.get('sequence')) is int for row in rows) and
            [row.get('sequence') for row in rows] == list(range(1, len(rows) + 1)), 'journey sequence mismatch')
    required = ('app.entry', 'launch', 'sdk.activation', 'sdk.managerCreated', 'sdk.incomingReported',
                'sdk.ringing', 'sdk.joinSettled', 'sdk.backendConnectedRequested',
                'sdk.joined', 'sdk.ended', 'sdk.result', 'done')
    ordered = [one(rows, event) for event in required]
    require([rows.index(row) for row in ordered] == sorted(rows.index(row) for row in ordered), 'journey order mismatch')
    require(one(rows, 'sdk.activation').get('active') is True, 'built-in simulator runtime inactive')
    require(one(rows, 'sdk.result').get('success') is True and
            not any(row['event'] in ('sdk.timeout', 'sdk.error') or row.get('error') for row in rows),
            'SDK journey failed or timed out')
    conversation = one(rows, 'sdk.incomingReported').get('conversationID')
    require(isinstance(conversation, str) and str(uuid.UUID(conversation)).upper() == conversation.upper(), 'invalid conversation UUID')
    require(all(row.get('conversationID') == conversation for row in rows if row['event'] in
                ('sdk.incomingReported', 'sdk.ringing', 'sdk.joinSettled', 'sdk.backendConnectedRequested',
                 'sdk.joined', 'sdk.ended', 'sdk.action', 'sdk.state')),
            'mixed or missing conversation identity')
    require(one(rows, 'sdk.ringing')['t_ms'] - one(rows, 'sdk.incomingReported')['t_ms'] >= 950,
            'incoming call did not survive the acceptance interval')
    actions = [row for row in rows if row['event'] == 'sdk.action']
    require([row.get('kind') for row in actions] == ['JoinConversationAction', 'EndConversationAction'], 'unexpected action inventory')
    settled = one(rows, 'sdk.joinSettled')
    backend = one(rows, 'sdk.backendConnectedRequested')
    joined = one(rows, 'sdk.joined')
    require(rows.index(one(rows, 'sdk.ringing')) < rows.index(actions[0]) < rows.index(settled) <
            rows.index(backend) < rows.index(joined) < rows.index(actions[1]) < rows.index(one(rows, 'sdk.ended')),
            'action/signaling/backend completion order mismatch')
    snapshots = [row for row in rows if row['event'] == 'sdk.snapshot']
    require(snapshots and all(type(row.get('revision')) is int and row['revision'] >= 0 for row in snapshots) and
            all(a['revision'] < b['revision'] for a, b in zip(snapshots, snapshots[1:])),
            'missing or invalid client snapshot revisions')
    for row in snapshots:
        calls = row.get('conversations')
        require(isinstance(calls, list) and len(calls) <= 1 and
                all(call.get('id') == conversation and type(call.get('hasConnectedAt')) is bool and
                    call.get('status') in ('ringing', 'connecting', 'connected', 'held', 'ending', 'ended')
                    for call in calls), 'unbound or invalid client snapshot')
    before_settled = [row for row in snapshots if
                      rows.index(actions[0]) < rows.index(row) < rows.index(settled) and row['conversations']]
    require(before_settled and before_settled[-1]['conversations'][0].get('status') == 'connecting' and
            before_settled[-1]['conversations'][0].get('pending') is None,
            'join settlement lacks a settled connecting snapshot')
    require(all(call['hasConnectedAt'] is False and call['status'] not in ('connected', 'held')
                for row in snapshots if rows.index(row) < rows.index(backend) for call in row['conversations']),
            'join signaling fabricated transport connection')
    connected = [row for row in snapshots if any(call['status'] == 'connected' for call in row['conversations'])]
    require(connected and rows.index(backend) < rows.index(connected[0]) < rows.index(joined) and
            connected[0]['conversations'][0].get('pending') is None and
            all(call['hasConnectedAt'] is True for row in connected for call in row['conversations']),
            'connected projection lacks the explicit backend report')
    for state, after, before in (('idle', 'sdk.managerCreated', 'sdk.ringing'),
                                 ('joined', 'sdk.ringing', 'sdk.joined'), ('left', 'sdk.joined', 'sdk.ended')):
        require(any(row['event'] == 'sdk.state' and row.get('state') == state and
                    rows.index(one(rows, after)) < index < rows.index(one(rows, before)) for index, row in enumerate(rows)),
                'missing LCK state ' + state)
    for case in CASES:
        bound_rows(root, 'bootstrap-' + case, manifest, 'dev.callkitsim.sdkbootstrap')
    bootstrap = bootstrap_conformance(root)
    return dict(success=True, scenario='sdk-m0', conversationID=conversation,
                simulatorOS=environment['simulatorOS'], profile='ios-27.0',
                bootstrap=bootstrap['cases'], deviceRuntimeImplementationAbsent=True,
                swiftPMDeviceObjects=len(objects), manifestSHA256=digest(root / 'manifest.json'))


def check_symbols(root, relative, expected, names):
    path = root / (relative + '.symbols')
    require(digest(path) == expected and path.read_text() == '\n'.join(names) + ('\n' if names else ''),
            'symbol evidence mismatch: ' + relative)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path)
    parser.add_argument('--begin-build', type=Path)
    parser.add_argument('--record-build', type=Path)
    parser.add_argument('--build-start', type=Path)
    parser.add_argument('--app', type=Path)
    parser.add_argument('--bootstrap', type=Path)
    parser.add_argument('--device-probe', type=Path)
    parser.add_argument('--spm-device-dir', type=Path)
    args = parser.parse_args()
    if sum(value is not None for value in (args.root, args.begin_build, args.record_build)) != 1:
        parser.error('choose an evidence directory, --begin-build or --record-build')
    try:
        if args.begin_build:
            begin_build(args.begin_build)
            result = dict(success=True, sourceSnapshot=True)
        elif args.record_build:
            if any(value is None for value in (args.build_start, args.app, args.bootstrap, args.device_probe, args.spm_device_dir)):
                parser.error('--record-build requires --build-start and all four built products')
            record_build(args.record_build, args.build_start, args.app, args.bootstrap, args.device_probe, args.spm_device_dir)
            result = dict(success=True, nativeBuildReceipt=True)
        else:
            result = validate(args.root)
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as error:
        result = dict(success=False, error=str(error))
    print(json.dumps(result, indent=2))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
