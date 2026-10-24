#!/usr/bin/env python3
"""Pure controls of the optional real-backend build inspector; no native tools."""
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'discovery'))
from build_sdk_device_release import inspect_livekit, inspect_standard_webrtc
from device_release_exclusion import ExclusionError


class Controls(unittest.TestCase):
    def inspect(self, *, edit=None, symbols=None):
        with tempfile.TemporaryDirectory(prefix='standard-webrtc-build-control-') as directory:
            root = Path(directory); app = root / 'Example.app'; app.mkdir()
            (app / 'Info.plist').write_bytes(plistlib.dumps({'VoIPSDK': {'mediaAdapter': 'webrtc'}}))
            (root / 'Podfile.properties.json').write_text(json.dumps({'ios-voip-sdk.mediaAdapter': 'webrtc'}))
            (root / 'Podfile.lock').write_text('PODS:\n  - JitsiWebRTC (155.0.1)\n  - react-native-webrtc (155.0.1):\n')
            images = {'Example': {'sha256': 'a' * 64}, 'Frameworks/WebRTC.framework/WebRTC': {'sha256': 'b' * 64}}
            if edit: edit(root, app)
            def run(command, **_):
                self.assertEqual(command[:3], ['xcrun', 'nm', '-jU'])
                value = (symbols or {}).get(Path(command[-1]).name,
                    '_OBJC_CLASS_$_RTCAudioSession\n' if Path(command[-1]).name == 'WebRTC' else '')
                return subprocess.CompletedProcess(command, 0, value, '')
            with patch('subprocess.run', side_effect=run), patch('subprocess.Popen', side_effect=AssertionError('native forbidden')):
                return inspect_standard_webrtc(app, {'appBinaries': images}, root)

    def test_pinned_defined_backend_is_build_proof_only(self):
        result = self.inspect()
        self.assertEqual(result['definedClassImage'], 'Frameworks/WebRTC.framework/WebRTC')
        self.assertIn('no execution or audio acceptance', result['qualification'])

    def test_absent_or_duplicate_defined_class_fails(self):
        for symbols in ({'WebRTC': ''}, {'Example': '_OBJC_CLASS_$_RTCAudioSession\n'}):
            with self.assertRaises(ExclusionError): self.inspect(symbols=symbols)

    def test_wrong_pins_and_other_fork_fail(self):
        for name in ('JitsiWebRTC', 'react-native-webrtc'):
            for value in ('155.0.2', '150.1.0'):
                def edit(root, app):
                    lock = root / 'Podfile.lock'
                    lock.write_text(lock.read_text().replace(name + ' (155.0.1)', name + ' (' + value + ')'))
                with self.assertRaises(ExclusionError): self.inspect(edit=edit)
        with self.assertRaises(ExclusionError):
            self.inspect(edit=lambda root, app: (root / 'Podfile.lock').write_text(
                (root / 'Podfile.lock').read_text() + '  - LiveKitWebRTC (150.7871.03)\n'))

    def test_generated_selection_must_match(self):
        for filename in ('Podfile.properties.json', 'Info.plist'):
            def edit(root, app):
                if filename.endswith('json'):
                    (root / filename).write_text(json.dumps({'ios-voip-sdk.mediaAdapter': 'none'}))
                else:
                    (app / filename).write_bytes(plistlib.dumps({'VoIPSDK': {'mediaAdapter': 'none'}}))
            with self.assertRaises(ExclusionError): self.inspect(edit=edit)


class LiveKitControls(unittest.TestCase):
    def inspect(self, *, edit=None, symbols=None):
        with tempfile.TemporaryDirectory(prefix='livekit-build-control-') as directory:
            root = Path(directory); app = root / 'Example.app'; app.mkdir()
            (app / 'Info.plist').write_bytes(plistlib.dumps({'VoIPSDK': {'mediaAdapter': 'livekit'}}))
            (root / 'Podfile.properties.json').write_text(json.dumps({'ios-voip-sdk.mediaAdapter': 'livekit'}))
            (root / 'Podfile.lock').write_text('PODS:\n  - livekit-react-native (3.1.0):\n'
                '  - livekit-react-native-webrtc (150.1.0):\n  - LiveKitWebRTC (150.7871.03)\n')
            images = {'Example': {}, 'Frameworks/LiveKitWebRTC.framework/LiveKitWebRTC': {}}
            if edit: edit(root, app)
            def run(command, **_):
                self.assertEqual(command[:3], ['xcrun', 'nm', '-jU'])
                defaults = {'Example': '_OBJC_CLASS_$_WebRTCModule\n',
                            'LiveKitWebRTC': '_OBJC_CLASS_$_LKRTCAudioDeviceModule\n'}
                return subprocess.CompletedProcess(command, 0,
                    (symbols or defaults).get(Path(command[-1]).name, ''), '')
            with patch('subprocess.run', side_effect=run), patch('subprocess.Popen', side_effect=AssertionError('native forbidden')):
                return inspect_livekit(app, {'appBinaries': images}, root)

    def test_actual_backend_build_remains_not_engine_acceptance(self):
        result = self.inspect()
        self.assertEqual(set(result['definedClassImages']), {'WebRTCModule', 'LKRTCAudioDeviceModule'})
        self.assertIn('no execution or audio acceptance', result['qualification'])

    def test_wrong_pin_opposite_provider_and_selection_fail(self):
        for replacement in ('150.1.1', '150.2.0'):
            def edit(root, app):
                path = root / 'Podfile.lock'; path.write_text(path.read_text().replace('150.1.0', replacement))
            with self.assertRaises(ExclusionError): self.inspect(edit=edit)
        with self.assertRaises(ExclusionError):
            self.inspect(edit=lambda root, app: (root / 'Podfile.lock').write_text(
                (root / 'Podfile.lock').read_text() + '  - react-native-webrtc (155.0.1):\n'))
        with self.assertRaises(ExclusionError):
            self.inspect(edit=lambda root, app: (root / 'Podfile.properties.json').write_text(
                json.dumps({'ios-voip-sdk.mediaAdapter': 'none'})))

    def test_missing_or_duplicate_native_classes_fail(self):
        for symbols in ({'Example': '_OBJC_CLASS_$_WebRTCModule\n'},
                        {'Example': '_OBJC_CLASS_$_WebRTCModule\n_OBJC_CLASS_$_LKRTCAudioDeviceModule\n',
                         'LiveKitWebRTC': '_OBJC_CLASS_$_LKRTCAudioDeviceModule\n'}):
            with self.assertRaises(ExclusionError): self.inspect(symbols=symbols)


if __name__ == '__main__':
    unittest.main()
