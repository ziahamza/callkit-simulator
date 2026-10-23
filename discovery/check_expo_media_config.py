#!/usr/bin/env python3
"""Evaluate actual CocoaPods source/dependency selection; no install or build."""
from pathlib import Path
import hashlib
import json
import subprocess
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ('IosVoipSDK.podspec', 'package.json', 'VERSION', 'discovery/check_expo_media_config.py')


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def main():
    before = hashes()
    checks = []
    with tempfile.TemporaryDirectory(prefix='ios-voip-media-config-') as temporary:
        # CocoaPods evaluates an IPC podspec from its containing directory. Use
        # byte-identical owned inputs rather than editing this checkout's config.
        for name in ('IosVoipSDK.podspec', 'package.json', 'VERSION'):
            shutil.copyfile(ROOT / name, Path(temporary) / name)
        properties = Path(temporary) / 'Podfile.properties.json'
        for adapter in (None, 'webrtc', 'livekit', 'none', 'invalid'):
            properties.write_text(json.dumps({} if adapter is None else {'ios-voip-sdk.mediaAdapter': adapter}))
            result = subprocess.run(['pod', 'ipc', 'spec', str(Path(temporary) / 'IosVoipSDK.podspec')],
                                    cwd=temporary, capture_output=True, text=True, timeout=30)
            if adapter == 'invalid':
                assert result.returncode != 0 and 'must be webrtc, livekit, or none' in result.stdout + result.stderr
                checks.append('invalid-pod-selection-fails')
                continue
            assert result.returncode == 0, result.stderr
            spec = json.loads(result.stdout)
            selected = adapter or 'webrtc'
            directory = {'webrtc': 'WebRTC', 'livekit': 'LiveKit', 'none': 'None'}[selected]
            expected = ['Sources/**/*.{swift,h,m}', 'Expo/apple/**/*.{swift,h}',
                        'ExpoMedia/Shared/ExpoMediaSupport.swift', f'ExpoMedia/{directory}/**/*.{{swift,h,m}}']
            if selected != 'none':
                expected += ['ExpoMedia/Shared/VSMediaBinding.h', 'ExpoMedia/Shared/QueuedMediaAdapter.swift']
            assert spec['source_files'] == expected
            dependencies = spec['dependencies']
            expected_dependencies = {'ExpoModulesCore': []}
            if selected == 'livekit':
                expected_dependencies.update({'React-Core': [], 'livekit-react-native-webrtc': ['= 150.1.0']})
            assert dependencies == expected_dependencies
            assert ('ExpoMedia/Shared/VSMediaBinding.h' in spec['public_header_files']) == (selected != 'none')
            assert spec['platforms'] == {'ios': '17.4'}
            checks.append(f'{adapter or "default"}-one-source-and-dependency-selection')
    assert 'ExpoMedia' in json.loads((ROOT / 'package.json').read_text())['files']
    assert hashes() == before, 'Sources changed during CocoaPods evaluation.'
    print(json.dumps({'success': True, 'hostOnly': True, 'checks': checks, 'sourceSHA256': before}, indent=2))


if __name__ == '__main__':
    main()
