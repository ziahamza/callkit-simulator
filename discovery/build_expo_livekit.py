#!/usr/bin/env python3
"""Build the pinned Expo local-demand fixture; never launch an app/simulator."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

from expo_livekit_conformance import product_files, validate_build
from sdk_m0_conformance import ROOT, source_identity


def command(args, log, timeout):
    with log.open('ab') as stream:
        result = subprocess.run(list(map(str, args)), cwd=ROOT, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError('build failed; inspect ' + str(log))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='fresh task-owned build directory')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = source_identity()
    sdk_version = subprocess.check_output(
        ['xcrun', '--sdk', 'iphonesimulator', '--show-sdk-version'], text=True, timeout=30).strip()
    if int(sdk_version.split('.')[0]) < 27:
        raise RuntimeError('the SDK consumer requires Apple SDK 27 or newer')
    ios = ROOT / 'example/ios'
    workspaces = sorted(ios.glob('*.xcworkspace'))
    if len(workspaces) != 1:
        raise RuntimeError('prebuild the livekit example and install its pods first')
    derived = output / 'DerivedData'
    command(['xcodebuild', '-workspace', workspaces[0], '-scheme', workspaces[0].stem,
             '-configuration', 'Release', '-destination', 'generic/platform=iOS Simulator',
             '-derivedDataPath', derived, 'ARCHS=' + subprocess.check_output(['uname', '-m'], text=True).strip(),
             'ONLY_ACTIVE_ARCH=YES', 'CODE_SIGNING_ALLOWED=NO', 'COMPILER_INDEX_STORE_ENABLE=NO', 'build'],
            output / 'build.log', 2400)
    apps = sorted((derived / 'Build/Products/Release-iphonesimulator').glob('*.app'))
    if len(apps) != 1:
        raise RuntimeError('require one Release example app')
    shutil.copytree(apps[0], output / 'example.app')
    sdk_path = subprocess.check_output(
        ['xcrun', '--sdk', 'iphonesimulator', '--show-sdk-path'], text=True, timeout=30).strip()
    arch = subprocess.check_output(['uname', '-m'], text=True).strip()
    command(['xcrun', '--sdk', 'iphonesimulator', 'clang', '-target', arch + '-apple-ios17.4-simulator',
             '-isysroot', sdk_path, ROOT / 'Tools/cksim-notify.c', '-o', output / 'cksim-notify'],
            output / 'notifier.log', 90)
    command(['codesign', '-s', '-', '--force', output / 'cksim-notify'], output / 'notifier.log', 30)
    for name, source in {
        'Podfile.lock': ios / 'Podfile.lock',
        'Podfile.properties.json': ios / 'Podfile.properties.json',
        'IosVoipSDK.podspec.json': ios / 'Pods/Local Podspecs/IosVoipSDK.podspec.json',
        'ios-27.0.json': ROOT / 'Sources/Engine/Resources/ios-27.0.json',
    }.items():
        shutil.copyfile(source, output / name)
    versions = {name: json.loads((ROOT / 'node_modules' / name / 'package.json').read_text())['version']
                for name in ('@livekit/react-native', '@livekit/react-native-webrtc', 'livekit-client', 'react-dom')}
    after = source_identity()
    if before != after:
        raise RuntimeError('source changed during the consumer build')
    info = dict(schemaVersion=1, product='expo-livekit', configuration='Release',
                sourceBefore=before, sourceAfter=after, sdkVersion=sdk_version,
                versions=versions, files=product_files(output))
    (output / 'build.json').write_text(json.dumps(info, indent=2) + '\n')
    validate_build(output)
    print(json.dumps(dict(success=True, classification='built only; native engine journeys pending',
                          source=after, files=info['files']), indent=2))


if __name__ == '__main__':
    main()
