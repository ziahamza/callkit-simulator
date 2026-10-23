#!/usr/bin/env python3
"""Compile and exercise the real Expo relay on the host, without Expo or LCK."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCES = (
    'Sources/VoIPSDK/ConversationModels.swift',
    'Sources/VoIPSDK/DialRequest.swift',
    'Sources/VoIPSDK/ConversationHandler.swift',
    'Sources/VoIPSDK/PushService.swift',
    'Sources/VoIPSDK/AudioRoutes.swift',
    'Expo/apple/ConversationBridge.swift',
    'Tests/ExpoBridge/OwnerDoubles.swift',
    'Tests/ExpoBridge/BridgeTests.swift',
)


def main():
    def source_hashes():
        return {source: hashlib.sha256((ROOT / source).read_bytes()).hexdigest()
                for source in (*SOURCES, 'discovery/check_expo_bridge.py')}
    before = source_hashes()
    with tempfile.TemporaryDirectory(prefix='ios-voip-expo-bridge-') as temporary:
        binary = Path(temporary) / 'BridgeTests'
        subprocess.run(['swiftc', '-swift-version', '6', '-warnings-as-errors', '-parse-as-library',
                        *(str(ROOT / source) for source in SOURCES), '-o', str(binary)],
                       check=True, timeout=60)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=25)
        if result.stderr:
            print(result.stderr, file=sys.stderr, end='')
        if result.returncode != 0 or 'SWIFT TASK CONTINUATION MISUSE' in result.stderr:
            print(result.stdout, end='')
            return result.returncode or 1
        receipt = json.loads(result.stdout)
        if receipt.get('success') is not True or not receipt.get('checks') or source_hashes() != before:
            raise ValueError('Missing host check result or source changed during compilation/execution.')
        receipt.update(hostOnly=True, sourceSHA256=before)
        print(json.dumps(receipt, indent=2))
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Expo relay host check failed: {error}', file=sys.stderr)
        raise SystemExit(1)
