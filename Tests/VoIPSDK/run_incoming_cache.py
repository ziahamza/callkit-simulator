#!/usr/bin/env python3
"""Compile the whole production client and check cache at public await boundaries."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
MODES = ('reported', 'joined-success', 'joined-sibling-success', 'joined-failure')
PLATFORM_GUARD = '#if canImport(LiveCommunicationKit) && (os(iOS) || os(watchOS))'
INPUTS = [
    'Sources/VoIPSDK/ConversationClient.swift',
    'Sources/VoIPSDK/IncomingReportTransport.swift',
    'Sources/VoIPSDK/ConversationModels.swift',
    'Sources/VoIPSDK/ConversationRecord.swift',
    'Sources/VoIPSDK/ConversationHandler.swift',
    'Sources/VoIPSDK/IncomingInvitationGate.swift',
    'Sources/VoIPSDK/IncomingIntake.swift',
    'Sources/VoIPSDK/PushWire.swift',
    'Sources/VoIPSDK/SimulatorSupport.swift',
    'Sources/VoIPSDK/ProcessSDKOwnership.swift',
    'Sources/VoIPSDK/AudioActivation.swift',
    'Sources/VoIPSDK/AudioRoutes.swift',
    'Sources/VoIPSDK/AudioAdapterOperations.swift',
    'Sources/VoIPSDK/AudioSessionQueue.swift',
    'Sources/VoIPSDK/MediaAdapter.swift',
    'Sources/VoIPPlatform/VSProcessSDKOwnership.m',
    'Sources/VoIPPlatform/include/VSProcessSDKOwnership.h',
    'Tests/VoIPSDK/IncomingCachePlatform.swift',
    'Tests/VoIPSDK/IncomingCacheTests.swift',
    'Tests/VoIPSDK/run_incoming_cache.py',
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def host_boundary(source, audio=False):
    framework = '@preconcurrency import LiveCommunicationKit'
    assert source.count(PLATFORM_GUARD) == source.count(framework) == 1
    source = source.replace(PLATFORM_GUARD, '#if true', 1).replace(framework, '', 1)
    if audio:
        assert source.count('import AVFAudio') == 1
        source = source.replace('import AVFAudio', '', 1)
    return source


def run(out):
    sources = {name: (ROOT / name).read_bytes() for name in INPUTS}
    receipt = dict(success=False, classification='host whole-client cache contracts with actual Swift/ObjC ownership; modeled LCK timing, no device run',
                   sourceSHA256={name: sha(data) for name, data in sources.items()}, commands=[], results=[])

    def command(label, argv, timeout):
        started = time.monotonic()
        try:
            completed = subprocess.run(argv, cwd=ROOT, capture_output=True, timeout=timeout)
            code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as error:
            code, stdout, stderr = 'timeout', error.stdout or b'', error.stderr or b''
        (out / (label + '.stdout')).write_bytes(stdout)
        (out / (label + '.stderr')).write_bytes(stderr)
        receipt['commands'].append(dict(label=label, exitCode=code,
            elapsedSeconds=round(time.monotonic() - started, 6), stdoutSHA256=sha(stdout), stderrSHA256=sha(stderr)))
        return code, stdout, stderr

    try:
        for filename in ('ConversationClient.swift', 'IncomingReportTransport.swift'):
            source = sources['Sources/VoIPSDK/' + filename].decode()
            (out / filename).write_text(host_boundary(source, audio=filename == 'ConversationClient.swift'))
        adapter = sources['Sources/VoIPSDK/MediaAdapter.swift'].decode()
        adapter_guard = '#if canImport(AVFAudio) && (os(iOS) || os(watchOS))'
        assert adapter.count(adapter_guard) == adapter.count('import AVFAudio') == 1
        (out / 'MediaAdapter.swift').write_text(adapter.replace(adapter_guard, '#if true', 1).replace('import AVFAudio', '', 1))
        compile_inputs = [str(ROOT / name) for name in INPUTS if name.endswith('.swift') and not
                          name.endswith(('/ConversationClient.swift', '/IncomingReportTransport.swift', '/MediaAdapter.swift'))]
        # Ownership uses Foundation only, so compile the real gate rather than
        # adding another modeled process-claim implementation to the facade.
        owner_object = out / 'VSProcessSDKOwnership.o'
        code, _, stderr = command('compile-ownership', ['xcrun', 'clang', '-fobjc-arc', '-fmodules',
            '-c', str(ROOT / 'Sources/VoIPPlatform/VSProcessSDKOwnership.m'),
            '-o', str(owner_object)], 30)
        if code != 0:
            raise RuntimeError('ownership host compile failed: ' + str(code) + '; ' + stderr.decode(errors='replace'))
        code, _, stderr = command('compile', ['xcrun', 'swiftc', '-swift-version', '6', '-warnings-as-errors',
            '-parse-as-library', '-import-objc-header',
            str(ROOT / 'Sources/VoIPPlatform/include/VSProcessSDKOwnership.h'),
            *compile_inputs, str(out / 'ConversationClient.swift'),
            str(out / 'IncomingReportTransport.swift'), str(out / 'MediaAdapter.swift'), str(owner_object),
            '-o', str(out / 'IncomingCacheTests')], 90)
        if code != 0:
            raise RuntimeError('whole-client cache compile failed: ' + str(code) + '; ' + stderr.decode(errors='replace'))
        for mode in MODES:
            code, stdout, _ = command(mode, [str(out / 'IncomingCacheTests'), mode], 10)
            result = json.loads(stdout)
            if result.get('mode') != mode or type(result.get('success')) is not bool:
                raise ValueError('missing or unbound caller observation: ' + mode)
            receipt['results'].append(dict(exitCode=code, observation=result))
        if any((ROOT / name).read_bytes() != data for name, data in sources.items()):
            raise RuntimeError('source changed during host cache measurement')
        receipt['success'] = all(item['exitCode'] == 0 and item['observation']['success'] is True for item in receipt['results'])
    except (OSError, ValueError, RuntimeError) as error:
        receipt['failure'] = str(error)
    finally:
        receipt['artifactSHA256'] = {p.name: sha(p.read_bytes()) for p in out.iterdir() if p.is_file() and p.name != 'receipt.json'}
        (out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt['success'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, help='fresh nonexistent directory for retained evidence')
    args = parser.parse_args()
    os.umask(0o077)
    if args.out:
        out = args.out.absolute()
        if out.exists() or not out.parent.is_dir(): parser.error('--out must be fresh with an existing parent')
        out.mkdir(mode=0o700)
        return run(out)
    with tempfile.TemporaryDirectory(prefix='ios-voip-incoming-cache-') as directory:
        return run(Path(directory))


if __name__ == '__main__':
    raise SystemExit(main())
