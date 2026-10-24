import json
from pathlib import Path
import runpy
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

CLI = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'Tools/cksim'))
RUNNER = runpy.run_path(str(Path(__file__).with_name('run.py')))
REASONS = ('none', 'do-not-disturb', 'block-list', 'restricted-sharing-mode',
           'call-is-protected', 'sensitive-participants')


class IncomingFilterControls(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.session = dict(schemaVersion=1, runID='run', pid=123, bundleID='test.fixture')
        (self.root / 'session.json').write_text(json.dumps(self.session))
        (self.root / 'run').mkdir()
        (self.root / 'run/events.jsonl').write_text('')
        self.args = SimpleNamespace(bundle='test.fixture', runtime_dir=self.root, app_container=self.root)
        self.bridge = CLI['InspectorBridge'](self.args, 'device', self.root)

    def test_each_filter_uses_the_existing_session_pinned_mailbox(self):
        control = Mock(return_value={'success': True})
        command = self.bridge.command
        with patch.dict(command.__globals__, control=control):
            for reason in REASONS:
                result = command(dict(operation='incoming-filter', reason=reason))
                self.assertTrue(result['success'])
                args, device = control.call_args.args
                self.assertEqual((args.operation, args.reason, device), ('incoming-filter', reason, 'device'))
                self.assertEqual(control.call_args.kwargs['expected_session'], self.session)
                self.assertIsNone(args.command_receipt)

    def test_filter_rejects_missing_unknown_and_extraneous_parameters(self):
        command = self.bridge.command
        for value in (dict(operation='incoming-filter'),
                      dict(operation='incoming-filter', reason='unknown'),
                      dict(operation='incoming-filter', reason=3),
                      dict(operation='incoming-filter', reason='none', faults={}),
                      dict(operation='incoming-filter', reason='none', callUUID='other')):
            with self.assertRaisesRegex(RuntimeError, 'filter'):
                command(value)

    def test_cli_flag_reaches_the_filter_operation(self):
        main = CLI['main']
        control = Mock(return_value={'success': True})
        with patch.dict(main.__globals__, control=control, validate_package=Mock(), simulator=Mock(return_value='device')):
            with patch('sys.argv', ['cksim', '--bundle', 'test.fixture', 'incoming-filter', '--reason', 'do-not-disturb']):
                self.assertEqual(main(), 0)
        self.assertEqual(control.call_args.args[0].reason, 'do-not-disturb')

    def test_filter_publication_uses_the_cached_mailbox_without_simulator_rpc(self):
        container = self.root / 'container'
        channel = container / 'Documents/.callkit-simulator'
        mailbox = channel / 'run'
        for name in ('requests', 'responses'):
            (mailbox / name).mkdir(parents=True)
        session = dict(self.session, controlTransport='directory-watch-v1')
        (channel / 'session.json').write_text(json.dumps(session))
        args = SimpleNamespace(bundle='test.fixture', runtime_dir=self.root, app_container=container,
                               operation='incoming-filter', reason='do-not-disturb', timeout=1,
                               command_receipt=None)
        elapsed = [0]
        requests = []
        def respond(_):
            elapsed[0] += 0.1
            request = json.loads(next((mailbox / 'requests').glob('*.json')).read_text())
            requests.append(request)
            (mailbox / 'responses' / (request['id'] + '.json')).write_text(json.dumps(dict(request, success=True)))
        control = CLI['control']
        rpc = Mock(side_effect=AssertionError('simulator RPC unavailable'))
        clock = SimpleNamespace(monotonic=lambda: elapsed[0], sleep=respond)
        with patch.dict(control.__globals__, target_container=Mock(return_value=container), simctl=rpc, time=clock):
            self.assertTrue(control(args, 'device', expected_session=session)['success'])
        self.assertEqual(requests[0]['reason'], 'do-not-disturb')
        self.assertEqual(requests[0]['operation'], 'incoming-filter')
        self.assertNotIn('faults', requests[0])
        rpc.assert_not_called()

    def test_inspector_requires_profile_consistent_configuration_events(self):
        root = Path(__file__).resolve().parents[2]
        source = self.root / 'main.swift'
        source.write_text('''import Foundation
func event(_ fields: [String: Any]) -> RuntimeEvent {
  var value: [String: Any] = ["schemaVersion": 1, "sequence": 1, "runID": "run", "pid": 123,
    "bundleID": "test.fixture", "event": "incomingFilter.configured", "t_ms": 1]
  value.merge(fields) { _, new in new }
  return try! JSONDecoder().decode(RuntimeEvent.self, from: JSONSerialization.data(withJSONObject: value))
}
let valid: [String: Any] = ["reason": "do-not-disturb", "code": 3, "verification": "verified", "requestID": "request"]
precondition(InspectorIncomingFilterState(event: event(valid))?.reason == .doNotDisturb)
for fields in [["code": 4], ["verification": "unverified"], ["reason": "unknown"], ["requestID": ""]] as [[String: Any]] {
  precondition(InspectorIncomingFilterState(event: event(valid.merging(fields) { _, new in new })) == nil)
}
precondition(InspectorIncomingFilterState(event: event(["reason": "none", "code": 0,
  "verification": "not-applicable", "requestID": "clear"]))?.reason == InspectorIncomingFilter.none)
let ended = event(["event": "report.ended", "reason": 2])
let encoded = try! JSONSerialization.jsonObject(with: JSONEncoder().encode(ended)) as! [String: Any]
precondition(encoded["reason"] as? Int == 2, "legacy numeric end reason must survive the Inspector feed")
''')
        binary = self.root / 'filter-tests'
        compiled = subprocess.run(['swiftc', '-swift-version', '6', str(root / 'Inspector/RuntimeEvent.swift'),
                                   str(root / 'Inspector/IncomingFilter.swift'), str(source), '-o', str(binary)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        tested = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)

    def test_probe_filter_value_must_match_the_latest_visible_configuration(self):
        rows = [dict(self.session, sequence=1, event='incomingFilter.configured', reason='block-list', requestID='set')]
        receipt = dict(timeline=rows, conversations=[], cursor=1, incomingFilter='block-list')
        verify = RUNNER['verify_probe_journal']
        verify((receipt,), rows, self.session)
        with self.assertRaises(AssertionError):
            verify((dict(receipt, incomingFilter='do-not-disturb'),), rows, self.session)
        rows.append(dict(self.session, sequence=2, event='incomingFilter.configured', reason='none', requestID='clear'))
        verify((receipt,), rows, self.session)


if __name__ == '__main__':
    unittest.main()
