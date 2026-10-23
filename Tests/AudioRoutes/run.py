#!/usr/bin/env python3
"""Compile actual route transport against inert host facades; never use AV audio."""
from pathlib import Path
import platform
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TEST = ROOT / 'Tests/AudioRoutes'


def run(command, timeout=90):
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(' '.join(map(str, command)) + '\n' + result.stdout + result.stderr)
    return result


def main():
    with tempfile.TemporaryDirectory(prefix='ios-voip-route-transport-') as temporary:
        out = Path(temporary)
        source = (ROOT / 'Sources/VoIPSDK/SystemAudioSession.swift').read_text()
        guard = '#if canImport(AVFAudio) && (os(iOS) || os(watchOS))'
        if source.count(guard) != 1 or source.count('import AVFAudio') != 1 or '#if os(iOS)' not in source:
            raise RuntimeError('SystemAudioSession platform boundary changed; review the host facade')
        # Select only the platform/import boundary. The actual notification
        # debounce, publication generation and authorization policy stay intact.
        source = source.replace(guard, '#if true', 1).replace('import AVFAudio', '', 1)
        source = source.replace('#if os(iOS)', '#if true')
        (out / 'SystemAudioSession.swift').write_text(source)
        headers = ROOT / 'Sources/VoIPPlatform/include'
        (out / 'module.modulemap').write_text('module VoIPPlatform {\n' + ''.join(
            '  header "' + str(headers / name) + '"\n' for name in
            ('VSAudioSessionQueue.h', 'VSAudioSessionLifecycleObserver.h')) + '  export *\n}\n')
        prefix = out / 'LifecycleHostPrefix.h'
        prefix.write_text('#import <Foundation/Foundation.h>\n#import <TargetConditionals.h>\n'
                          '#undef TARGET_OS_IOS\n#define TARGET_OS_IOS 1\n'
                          '#undef TARGET_OS_WATCH\n#define TARGET_OS_WATCH 0\n'
                          '#undef TARGET_OS_SIMULATOR\n#define TARGET_OS_SIMULATOR 0\n'
                          '#undef __IPHONE_OS_VERSION_MAX_ALLOWED\n'
                          '#define __IPHONE_OS_VERSION_MAX_ALLOWED 270000\n')
        target = platform.machine() + '-apple-macos14.0'
        common = ['xcrun', 'clang', '-target', target, '-fobjc-arc', '-fmodules', '-fblocks',
                  '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter']
        objects = []
        for name, path, extra in (
            ('Queue', ROOT / 'Sources/VoIPPlatform/VSAudioSessionQueue.m', []),
            ('Observer', ROOT / 'Sources/VoIPPlatform/VSAudioSessionLifecycleObserver.m',
             ['-include', str(prefix), '-DVS_HOST_NEW_API=1',
              '-I' + str(ROOT / 'Tests/AudioLifecycle/Facades')]),
            ('Notifications', TEST / 'LifecycleNotifications.m',
             ['-DVS_HOST_NEW_API=1', '-I' + str(ROOT / 'Tests/AudioLifecycle/Facades')]),
        ):
            obj = out / (name + '.o')
            run([*common, *extra, '-c', str(path), '-o', str(obj)])
            objects.append(str(obj))
        binary = out / 'SessionTests'
        run(['xcrun', 'swiftc', '-target', target, '-swift-version', '6', '-warnings-as-errors',
             '-parse-as-library', '-I', str(out),
             str(ROOT / 'Sources/VoIPSDK/AudioSessionQueue.swift'),
             str(ROOT / 'Sources/VoIPSDK/AudioRoutes.swift'), str(out / 'SystemAudioSession.swift'),
             str(TEST / 'SessionFacade.swift'), str(TEST / 'SessionTests.swift'),
             *objects, '-framework', 'Foundation', '-o', str(binary)])
        result = run([str(binary)], timeout=20)
        if not result.stdout.startswith('passed:') or 'SWIFT TASK CONTINUATION MISUSE' in result.stderr:
            raise RuntimeError('Missing route transport success or leaked continuation\n' + result.stdout + result.stderr)
        print(result.stdout, end='')


if __name__ == '__main__':
    main()
