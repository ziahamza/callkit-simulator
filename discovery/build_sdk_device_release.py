#!/usr/bin/env python3
"""Build and inspect the generated Expo pod app for unsigned Release iphoneos."""
import argparse
import json
from pathlib import Path
import plistlib
import re
import subprocess

from device_release_exclusion import digest, inspect_app, require
from sdk_m0_conformance import ROOT, source_identity


def require_provider_configuration(ios):
    properties = json.loads((ios / 'Podfile.properties.json').read_text())
    require(properties.get('ios-voip-sdk.providerOwnershipMonitoring') == 'true', 'provider diagnostics pod property must be true')
    config = ios / 'Pods/Target Support Files/IosVoipSDK/IosVoipSDK.release.xcconfig'
    settings = dict(line.split('=', 1) for line in config.read_text().splitlines() if '=' in line)
    settings = {name.strip(): value.split() for name, value in settings.items()}
    for name, flag in [('SWIFT_ACTIVE_COMPILATION_CONDITIONS', 'VSSDK_ENABLE_PROVIDER_DIAGNOSTICS'),
                       ('GCC_PREPROCESSOR_DEFINITIONS', 'VSSDK_ENABLE_PROVIDER_DIAGNOSTICS=1')]:
        require(flag in settings.get(name, []), 'generated Release pod configuration is missing ' + flag)


def inspect_standard_webrtc(app, inspection, ios_root):
    """Additional build/link proof; never executes backend methods or the app."""
    require(plistlib.loads((app / 'Info.plist').read_bytes()).get('VoIPSDK', {}).get('mediaAdapter') == 'webrtc',
            'standard backend build must select webrtc')
    properties = json.loads((ios_root / 'Podfile.properties.json').read_text())
    require(properties.get('ios-voip-sdk.mediaAdapter') == 'webrtc', 'standard backend pod selection differs')
    lock = ios_root / 'Podfile.lock'
    contents = lock.read_text()
    for name in ('react-native-webrtc', 'JitsiWebRTC'):
        require(re.search(r'^  - ' + name + r' \(155\.0\.1\)(?::)?$', contents, re.MULTILINE),
                'standard backend must resolve pinned ' + name + ' 155.0.1')
    require('LiveKitWebRTC' not in contents and 'livekit-react-native-webrtc' not in contents,
            'standard backend build must not link the LiveKit fork')
    owners = []
    for name in inspection['appBinaries']:
        result = subprocess.run(['xcrun', 'nm', '-jU', str(app / name)], capture_output=True, text=True, timeout=30)
        require(result.returncode == 0, 'cannot inspect standard backend defined symbols')
        if '_OBJC_CLASS_$_RTCAudioSession' in result.stdout.splitlines(): owners.append(name)
    require(len(owners) == 1, 'require exactly one defined standard RTCAudioSession implementation')
    return dict(qualification='pinned real-backend build/link only; no execution or audio acceptance',
                npmVersion='155.0.1', podVersion='155.0.1', podfileLockSHA256=digest(lock),
                definedClassImage=owners[0], imageSHA256=inspection['appBinaries'][owners[0]]['sha256'])


def inspect_livekit(app, inspection, ios_root):
    """Selected Release device linkage; actual engine acceptance runs separately."""
    require(plistlib.loads((app / 'Info.plist').read_bytes()).get('VoIPSDK', {}).get('mediaAdapter') == 'livekit' and
            json.loads((ios_root / 'Podfile.properties.json').read_text()).get('ios-voip-sdk.mediaAdapter') == 'livekit',
            'LiveKit device build selections differ')
    lock = ios_root / 'Podfile.lock'
    contents = lock.read_text()
    for name, version in (('livekit-react-native', '3.1.0'), ('livekit-react-native-webrtc', '150.1.0'),
                          ('LiveKitWebRTC', '150.7871.03')):
        require(re.search(r'^  - ' + re.escape(name + ' (' + version + ')') + r'(?::)?$', contents, re.MULTILINE),
                'LiveKit device build must resolve pinned ' + name)
    require('JitsiWebRTC' not in contents and not re.search(r'^  - react-native-webrtc \(', contents, re.MULTILINE),
            'LiveKit device build linked the opposite WebRTC provider')
    owners = {name: [] for name in ('WebRTCModule', 'LKRTCAudioDeviceModule')}
    for image in inspection['appBinaries']:
        result = subprocess.run(['xcrun', 'nm', '-jU', str(app / image)], capture_output=True, text=True, timeout=30)
        require(result.returncode == 0, 'cannot inspect LiveKit defined symbols')
        for name in owners:
            if '_OBJC_CLASS_$_' + name in result.stdout.splitlines(): owners[name].append(image)
    require(all(len(images) == 1 for images in owners.values()), 'require exactly one existing RN module and LiveKit ADM')
    return dict(qualification='pinned real-backend build/link only; no execution or audio acceptance',
                podfileLockSHA256=digest(lock), definedClassImages=owners)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='fresh output directory')
    parser.add_argument('--derived-data', type=Path, help='optional existing task-owned build cache')
    parser.add_argument('--provider-diagnostics', action='store_true', help='build and inspect explicitly enabled Release diagnostics')
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument('--standard-webrtc', action='store_true', help='also require the example pinned standard backend')
    backend.add_argument('--livekit', action='store_true', help='also require the example pinned LiveKit backend')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = source_identity()
    (output / 'source-before.json').write_text(json.dumps(before, indent=2) + '\n')
    workspaces = sorted((ROOT / 'example/ios').glob('*.xcworkspace'))
    require(len(workspaces) == 1, 'generate one example workspace and install pods first')
    if args.provider_diagnostics:
        require_provider_configuration(ROOT / 'example/ios')
    command = ['xcodebuild', '-workspace', str(workspaces[0]), '-scheme', workspaces[0].stem,
               '-configuration', 'Release', '-destination', 'generic/platform=iOS',
               '-derivedDataPath', str((args.derived_data or output / 'DerivedData').resolve()), 'ARCHS=arm64',
               'ONLY_ACTIVE_ARCH=YES', 'CODE_SIGNING_ALLOWED=NO',
               'COMPILER_INDEX_STORE_ENABLE=NO', 'build']
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    with (output / 'build.log').open('wb') as stream:
        result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=2400)
    (output / 'build-status.json').write_text(json.dumps(dict(exitCode=result.returncode)) + '\n')
    require(result.returncode == 0, 'Release iphoneos build failed; inspect build.log')
    after = source_identity()
    require(before == after, 'source changed during the Release iphoneos build')
    products = (args.derived_data or output / 'DerivedData') / 'Build/Products/Release-iphoneos'
    apps = sorted(products.glob('*.app'))
    require(len(apps) == 1, 'expected exactly one Release iphoneos app')
    pod_binaries = sorted(set(products.rglob('libIosVoipSDK.a')) |
                          set(products.glob('**/IosVoipSDK.framework/IosVoipSDK')))
    require(len(pod_binaries) == 1, 'expected exactly one built IosVoipSDK pod binary')
    inspection = inspect_app(apps[0], pod_binaries[0], ROOT / 'example/ios/Pods/Headers/Public/IosVoipSDK',
                             args.provider_diagnostics)
    if args.standard_webrtc:
        inspection['standardWebRTC'] = inspect_standard_webrtc(apps[0], inspection, ROOT / 'example/ios')
    if args.livekit:
        inspection['livekit'] = inspect_livekit(apps[0], inspection, ROOT / 'example/ios')
    inspection.update(before, sourceAfter=after, configuration='Release', signingAllowed=False,
                      providerDiagnostics=args.provider_diagnostics,
                      buildExitCode=result.returncode, commandSHA256=digest(output / 'command.json'),
                      buildLogSHA256=digest(output / 'build.log'))
    (output / 'receipt.json').write_text(json.dumps(inspection, indent=2) + '\n')
    print(json.dumps(inspection, indent=2))


if __name__ == '__main__':
    main()
