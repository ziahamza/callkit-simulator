#!/usr/bin/env python3
"""Build a fresh pinned Swift consumer and simulator app; never launch either."""
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / 'Tests/SwiftLiveKit'
ADAPTER = ROOT / 'Integrations/LiveKit/LiveKitMediaAdapter.swift'
INPUTS = ('Tests/SwiftLiveKit/Package.swift', 'Tests/SwiftLiveKit/Package.resolved',
          'Tests/SwiftLiveKit/NativeJourney.swift', 'Integrations/LiveKit/LiveKitMediaAdapter.swift',
          'discovery/build_swift_livekit_contract.py', 'Package.swift', 'Tools/cksim-notify.c',
          'discovery/run_swift_livekit.py', 'discovery/swift_livekit_conformance.py',
          'discovery/check_swift_livekit.py', '.github/workflows/sdk-m0.yml')
PINS = {'client-sdk-swift': '93a7251057bdb6a80a7b1f6d11464d578ebdc09b',
        'webrtc-xcframework': 'a41df9e9bcc7ab1d62131fda75c123ea391e5188',
        'livekit-uniffi-xcframework': '7a9bf91b1601d5c408ae0cac7dc2047b5a4f615e'}
OBJECTS = ('CKSimRuntimeC', 'CKSimRuntime', 'VoIPPlatform', 'VoIPSDK', 'CLiveKitProto',
           'LiveKitNanopb', 'LKObjCHelpers', 'LiveKitUniFFI', 'LiveKit', 'LiveKitConsumer')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source():
    from sdk_m1_contracts_conformance import source_identity
    identity = source_identity(ROOT)
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--', *INPUTS], cwd=ROOT)
    if identity['sourceDirty'] or dirty:
        raise RuntimeError('Commit SDK and fixture inputs before building native acceptance')
    return {**identity, 'fixtureInputs': {name: digest(ROOT / name) for name in INPUTS}}


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def require_files(paths):
    for path in paths:
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError('Missing or empty build output: ' + str(path))


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: build_swift_livekit_contract.py FRESH_OUTPUT_DIRECTORY')
    out = Path(sys.argv[1]).resolve(); out.mkdir()
    before = source(); save(out / 'source-before.json', before)
    architecture = os.environ.get('CKSIM_BUILD_ARCH', platform.machine())
    target = architecture + '-apple-ios17.4-simulator'
    app = out / 'SDKSwiftLiveKit.app'
    with tempfile.TemporaryDirectory(prefix='.build-', dir=out) as temporary, (out / 'build.log').open('wb') as log:
        work = Path(temporary); consumer = work / 'consumer'; consumer.mkdir()
        products = work / 'DerivedData/Build/Products/Debug-iphonesimulator'
        maps = work / 'DerivedData/Build/Intermediates.noindex/GeneratedModuleMaps-iphonesimulator'

        def run(command, *, timeout=180, cwd=consumer, capture=False):
            log.write((json.dumps([str(value) for value in command]) + '\n').encode()); log.flush()
            result = subprocess.run([str(value) for value in command], cwd=cwd, check=True,
                stdout=subprocess.PIPE if capture else log, stderr=log, timeout=timeout)
            if capture:
                log.write(result.stdout); log.flush()
                return result.stdout.decode().strip()

        sdk = run(['xcrun', '--sdk', 'iphonesimulator', '--show-sdk-path'], timeout=30, capture=True)
        sdk_version = run(['xcrun', '--sdk', 'iphonesimulator', '--show-sdk-version'], timeout=30, capture=True)
        if int(sdk_version.split('.')[0]) < 27:
            raise RuntimeError('The consumer requires SDK 27 or later; minimum runtime remains 17.4')
        manifest = (FIXTURE / 'Package.swift').read_text()
        marker = 'let sdkRoot = "../.."'
        if manifest.count(marker) != 1:
            raise RuntimeError('Review the consumer local-package path substitution')
        (consumer / 'Package.swift').write_text(manifest.replace(marker,
            'let sdkRoot = ' + json.dumps(str(ROOT), ensure_ascii=False)))
        shutil.copyfile(FIXTURE / 'Package.resolved', consumer / 'Package.resolved')
        sources = consumer / 'Sources/LiveKitConsumer'; sources.mkdir(parents=True)
        shutil.copyfile(ADAPTER, sources / 'LiveKitMediaAdapter.swift')
        (sources / 'Consumer.swift').write_text('public enum LiveKitConsumerBuild {\n'
            '  public static let liveKitRevision = "' + PINS['client-sdk-swift'] + '"\n}\n')
        common = ['xcodebuild', '-scheme', 'LiveKitConsumer',
                  '-clonedSourcePackagesDirPath', work / 'SourcePackages']
        run([*common, '-resolvePackageDependencies'], timeout=600)
        resolved = json.loads((consumer / 'Package.resolved').read_text())
        actual = {pin['identity']: pin['state']['revision'] for pin in resolved['pins']}
        if actual != PINS:
            raise RuntimeError('Resolved LiveKit source or binary wrapper revision differs from input pins')
        shutil.copyfile(consumer / 'Package.resolved', out / 'Package.resolved')
        run([*common, '-destination', 'generic/platform=iOS Simulator', '-configuration', 'Debug',
             '-derivedDataPath', work / 'DerivedData', '-disableAutomaticPackageResolution',
             'ARCHS=' + architecture, 'ONLY_ACTIVE_ARCH=YES', 'CODE_SIGNING_ALLOWED=NO',
             'COMPILER_INDEX_STORE_ENABLE=NO', 'build'], timeout=1200)
        for name, revision in PINS.items():
            actual_revision = run(['git', '-C', work / 'SourcePackages/checkouts' / name,
                                   'rev-parse', 'HEAD'], timeout=30, capture=True)
            if actual_revision != revision:
                raise RuntimeError('Built dependency checkout differs: ' + name)
        objects = [products / (name + '.o') for name in OBJECTS]
        module_maps = [maps / (name + '.modulemap') for name in
                       ('VoIPPlatform', 'CKSimRuntimeC', 'CLiveKitProto', 'LKObjCHelpers')]
        require_files([*objects, *module_maps, products / 'LiveKit_LiveKit.bundle/Info.plist'])
        (app / 'Frameworks').mkdir(parents=True)
        # Keep the SwiftPM fallback/oracle bundle shape. Source SDK builds load
        # the embedded profile; this file does not prove which bytes were loaded.
        (app / 'VoIPSDK_CKSimRuntime.bundle').mkdir()
        shutil.copyfile(ROOT / 'Sources/Engine/Resources/ios-27.0.json', app / 'VoIPSDK_CKSimRuntime.bundle/ios-27.0.json')
        profile_info = dict(CFBundleIdentifier='dev.iosvoipsdk.swiftlivekit.runtime', CFBundlePackageType='BNDL', CFBundleVersion='1')
        (app / 'VoIPSDK_CKSimRuntime.bundle/Info.plist').write_bytes(plistlib.dumps(profile_info))
        info = plistlib.loads((ROOT / 'discovery/probe/Info.plist').read_bytes())
        info.update(CFBundleIdentifier='dev.iosvoipsdk.swiftlivekit', CFBundleExecutable='SDKSwiftLiveKit',
                    CFBundleName='SDKSwiftLiveKit', CFBundleDisplayName='SDK Swift LiveKit', MinimumOSVersion='17.4')
        (app / 'Info.plist').write_bytes(plistlib.dumps(info))
        for bundle in products.glob('*.bundle'):
            shutil.copytree(bundle, app / bundle.name, symlinks=True)
        for name in ('LiveKitWebRTC', 'RustLiveKitUniFFI'):
            require_files([products / (name + '.framework') / name])
            framework = app / 'Frameworks' / (name + '.framework')
            shutil.copytree(products / framework.name, framework, symlinks=True)
            run(['codesign', '-s', '-', '--force', framework], timeout=30)
        command = ['xcrun', '--sdk', 'iphonesimulator', 'swiftc', '-target', target, '-sdk', sdk,
                   '-swift-version', '6', '-D', 'DEBUG', '-parse-as-library', '-I', products, '-F', products]
        for module_map in module_maps:
            command += ['-Xcc', '-fmodule-map-file=' + str(module_map)]
        run([*command, FIXTURE / 'NativeJourney.swift', *objects,
             '-framework', 'LiveKitWebRTC', '-framework', 'RustLiveKitUniFFI',
             '-Xlinker', '-ObjC', '-Xlinker', '-rpath', '-Xlinker', '@executable_path/Frameworks',
             '-o', app / 'SDKSwiftLiveKit'], timeout=180)
        run(['codesign', '-s', '-', '--force', app], timeout=30)
        run(['xcrun', '--sdk', 'iphonesimulator', 'clang', '-target', target, '-isysroot', sdk,
             ROOT / 'Tools/cksim-notify.c', '-o', out / 'cksim-notify'], timeout=60)
        run(['codesign', '-s', '-', '--force', out / 'cksim-notify'], timeout=30)
        xcode_version = run(['xcodebuild', '-version'], timeout=30, capture=True)
    after = source(); save(out / 'source-after.json', after)
    if before != after:
        raise RuntimeError('SDK or fixture inputs changed during native build')
    bound_files = [app / 'SDKSwiftLiveKit', app / 'Info.plist',
        app / 'Frameworks/LiveKitWebRTC.framework/LiveKitWebRTC',
        app / 'Frameworks/RustLiveKitUniFFI.framework/RustLiveKitUniFFI',
        app / 'VoIPSDK_CKSimRuntime.bundle/ios-27.0.json', out / 'Package.resolved',
        out / 'build.log', out / 'source-before.json', out / 'source-after.json', out / 'cksim-notify']
    require_files(bound_files)
    save(out / 'build-info.json', dict(schemaVersion=1, product='sdk-swift-livekit-contract',
        configuration='Debug', target=target, sdkVersion=sdk_version, xcodeVersion=xcode_version,
        **after, resolvedPins=actual, files={str(path.relative_to(out)): digest(path)
        for path in bound_files}))
    print('Built ' + str(app) + '; no simulator or device was used.')


if __name__ == '__main__':
    main()
