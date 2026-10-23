#!/usr/bin/env python3
"""Compile the real platform observer and execute bounded host decoding controls.

Host facades select compiler branches; they do not measure notification delivery
on iOS/watchOS, runtime availability, or audio engine authorization.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TEST = ROOT / 'Tests/AudioLifecycle'
SOURCE = ROOT / 'Sources/VoIPPlatform/VSAudioSessionLifecycleObserver.m'
HEADER = ROOT / 'Sources/VoIPPlatform/include/VSAudioSessionLifecycleObserver.h'
LEGACY_TEST_FLAG = '-DVSSDK_TEST_AUDIO_LIFECYCLE_LEGACY_SDK=1'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command, results, name, *, expected_failure=False, timeout=90):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    results[name] = dict(command=command, exitCode=result.returncode,
                         stdout=result.stdout, stderr=result.stderr,
                         expectedFailure=expected_failure)
    if (result.returncode == 0) == expected_failure:
        raise RuntimeError(name + ' unexpected exit:\n' + result.stdout + result.stderr)
    return result


def main():
    inputs = [SOURCE, HEADER, *sorted(path for path in TEST.rglob('*') if path.is_file()
                                     and '__pycache__' not in path.parts)]
    snapshot = lambda: {str(path.relative_to(ROOT)): digest(path) for path in inputs}
    before = snapshot()
    for packaging in ('Package.swift', 'IosVoipSDK.podspec'):
        if 'VSSDK_TEST_AUDIO_LIFECYCLE_LEGACY_SDK' in (ROOT / packaging).read_text():
            raise RuntimeError('test-only older-SDK optout leaked into consumer packaging')
    results, sdk_inputs = {}, {}
    with tempfile.TemporaryDirectory(prefix='ios-voip-audio-lifecycle-') as temporary:
        out = Path(temporary)
        # Preimport Foundation before selecting a compile-only older SDK branch.
        # This does not replace installed headers or claim an older SDK was used.
        older = out / 'older-sdk-branch.h'
        older.write_text('#import <Foundation/Foundation.h>\n'
                         '#undef __IPHONE_OS_VERSION_MAX_ALLOWED\n'
                         '#define __IPHONE_OS_VERSION_MAX_ALLOWED 260000\n'
                         '#undef __WATCH_OS_VERSION_MAX_ALLOWED\n'
                         '#define __WATCH_OS_VERSION_MAX_ALLOWED 260000\n')
        for sdk_name, target in [('iphoneos', 'arm64-apple-ios17.4'),
                                 ('watchsimulator', 'arm64-apple-watchos10.0-simulator')]:
            sdk = run(['xcrun', '--sdk', sdk_name, '--show-sdk-path'], results,
                      sdk_name + '-path', timeout=20).stdout.strip()
            sdk_version = run(['xcrun', '--sdk', sdk_name, '--show-sdk-version'], results,
                              sdk_name + '-version', timeout=20).stdout.strip()
            current_has_new_api = int(sdk_version.split('.')[0]) >= 27
            audio_headers = Path(sdk) / 'System/Library/Frameworks/AVFAudio.framework/Headers'
            sdk_inputs[sdk_name] = {str(audio_headers / name): digest(audio_headers / name)
                                   for name in ('AVAudioSession.h', 'AVAudioSessionTypes.h')}
            for branch in ('current', 'older-compile-branch'):
                object_file = out / (sdk_name + '-' + branch + '.o')
                command = ['xcrun', 'clang', '-target', target, '-isysroot', sdk,
                           '-fobjc-arc', '-fblocks', '-Wall', '-Wextra', '-Werror',
                           '-Wno-unused-parameter', '-Wno-deprecated-declarations',
                           '-c', str(SOURCE), '-o', str(object_file)]
                if branch != 'current':
                    command += ['-include', str(older)]
                expects_new_api = branch == 'current' and current_has_new_api
                if not expects_new_api:
                    # Production refuses an old-SDK binary: it cannot silently
                    # register only legacy notifications when later run on 27.
                    refusal = run(command, results, sdk_name + '-' + branch + '-consumer-refused', expected_failure=True)
                    if 'VoIPSDK requires the iOS/watchOS 27 SDK' not in refusal.stderr:
                        raise RuntimeError('older-SDK consumer failed for an unrelated reason')
                    command += [LEGACY_TEST_FLAG]
                run(command, results, sdk_name + '-' + branch)
                symbols = run(['xcrun', 'nm', '-u', str(object_file)], results,
                              sdk_name + '-' + branch + '-symbols', timeout=20).stdout
                has_new_symbols = 'AVAudioSessionDidBecomeActiveNotification' in symbols
                expects_new_api = branch == 'current' and current_has_new_api
                if has_new_symbols != expects_new_api:
                    raise RuntimeError(sdk_name + ': SDK guard did not select the expected branch')
                if not expects_new_api and any(name in symbols for name in (
                        'AVAudioSessionDeactivationContext', 'AVAudioSessionResumptionContext',
                        'AVAudioSessionDidBecomeInactive', 'AVAudioSessionResumptionRecommendation')):
                    raise RuntimeError(sdk_name + ': older compiler branch referenced iOS/watchOS27 symbols')

        # The source-only diagnostic surface must disappear in every device
        # build and every non-Debug simulator build.
        for sdk_name, target, debug in [('iphonesimulator', 'arm64-apple-ios17.4-simulator', True),
                                        ('iphonesimulator', 'arm64-apple-ios17.4-simulator', False),
                                        ('iphoneos', 'arm64-apple-ios17.4', True),
                                        ('iphoneos', 'arm64-apple-ios17.4', False)]:
            label = sdk_name + ('-debug' if debug else '-release')
            sdk = run(['xcrun', '--sdk', sdk_name, '--show-sdk-path'], results,
                      label + '-diagnostic-sdk', timeout=20).stdout.strip()
            object_file = out / (label + '-diagnostic.o')
            command = ['xcrun', 'clang', '-target', target, '-isysroot', sdk,
                       '-fobjc-arc', '-fblocks', '-Wall', '-Wextra', '-Werror',
                       '-Wno-unused-parameter', '-Wno-deprecated-declarations',
                       '-c', str(SOURCE), '-o', str(object_file)]
            version = run(['xcrun', '--sdk', sdk_name, '--show-sdk-version'], results,
                          label + '-diagnostic-version', timeout=20).stdout.strip()
            if int(version.split('.')[0]) < 27: command += [LEGACY_TEST_FLAG]
            if debug: command += ['-DDEBUG=1']
            run(command, results, label + '-diagnostic-compile')
            symbols = run(['xcrun', 'nm', '-j', str(object_file)], results,
                          label + '-diagnostic-symbols', timeout=20).stdout
            if ('_VSAudioSessionSetLifecycleDiagnosticHandler' in symbols) != (debug and sdk_name == 'iphonesimulator'):
                raise RuntimeError(label + ': diagnostic guard differs')

        mac_sdk = run(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], results,
                      'macosx-path', timeout=20).stdout.strip()
        for new_api in (False, True):
            label = '27' if new_api else 'legacy'
            prefix = out / ('host-' + label + '.h')
            prefix.write_text('#import <Foundation/Foundation.h>\n'
                              '#import <TargetConditionals.h>\n'
                              '#undef TARGET_OS_IOS\n#define TARGET_OS_IOS 1\n'
                              '#undef TARGET_OS_WATCH\n#define TARGET_OS_WATCH 0\n'
                              '#undef TARGET_OS_SIMULATOR\n#define TARGET_OS_SIMULATOR 1\n'
                              '#undef __IPHONE_OS_VERSION_MAX_ALLOWED\n'
                              '#define __IPHONE_OS_VERSION_MAX_ALLOWED ' + ('270000' if new_api else '260000') + '\n'
                              '#undef __WATCH_OS_VERSION_MAX_ALLOWED\n'
                              '#define __WATCH_OS_VERSION_MAX_ALLOWED 0\n')
            binary = out / ('host-' + label)
            command = ['xcrun', 'clang', '-target', 'arm64-apple-macos14.0', '-isysroot', mac_sdk,
                       '-fobjc-arc', '-fblocks', '-Wall', '-Wextra', '-Werror',
                       '-Wno-unused-parameter', '-DDEBUG=1', '-DVS_HOST_NEW_API=' + ('1' if new_api else '0'),
                       '-include', str(prefix), '-I' + str(TEST / 'Facades'),
                       '-I' + str(HEADER.parent), '-I' + str(SOURCE.parent),
                       str(SOURCE), str(TEST / 'Controls.m'), '-framework', 'Foundation', '-o', str(binary)]
            if not new_api: command += [LEGACY_TEST_FLAG]
            run(command, results, 'host-' + label + '-compile')
            result = run([str(binary)], results, 'host-' + label + '-controls', timeout=15)
            if not result.stdout.startswith('passed:'):
                raise RuntimeError('missing host control completion marker')
            if new_api:
                mutation = out / 'activation-mutation.m'
                mutation.write_text(SOURCE.read_text().replace('_tokens = [NSMutableArray new];',
                    '_tokens = [NSMutableArray new]; [[AVAudioSession sharedInstance] setActive:YES error:nil];', 1))
                negative = command[:]
                negative[negative.index(str(SOURCE))] = str(mutation)
                result = run(negative, results, 'activation-mutation-refused', expected_failure=True)
                if 'AVAudioSession' not in result.stderr:
                    raise RuntimeError('activation mutation failed for an unrelated reason')
        if before != snapshot():
            raise RuntimeError('lifecycle source or controls changed while checking')
    print(json.dumps(dict(success=True,
        classification='Compiler/host decoder controls only. Pre-27 SDK optout is test-only; consumer builds require SDK27. Native delivery and OS availability branches are measured separately.',
        sourceFiles=before, installedSDKHeaders=sdk_inputs, checks=results,
        temporaryProductsRemoved=True, simulatorBooted=False, deviceUsed=False), indent=2))


if __name__ == '__main__':
    main()
