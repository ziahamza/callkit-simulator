"""Measured LCK app controls and explicit call-waiting targets use the pinned mailbox."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


CLI = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'Tools/cksim'))
CALL = 'A7E390AF-6B78-4190-B946-26D9C6438584'
ACTIVE = '91B69FD7-1B78-49BC-AC7D-A97ED81543DD'
HELD = 'D26CE594-689A-446A-9DED-A7AA7B78D2F6'


class LCKControlsTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.session = dict(schemaVersion=1, runID='run', pid=123, bundleID='test.fixture')
        (self.root / 'session.json').write_text(json.dumps(self.session))
        (self.root / 'run').mkdir()
        (self.root / 'run/events.jsonl').write_text('')
        self.bridge = CLI['InspectorBridge'](SimpleNamespace(bundle='test.fixture'), 'device', self.root)

    def test_new_controls_forward_explicit_targets_without_capability_or_membership_checks(self):
        send = self.bridge.command
        control = Mock(return_value=dict(success=True))
        cases = [dict(operation='hold', callUUID=CALL, held='true'),
                 dict(operation='hold', callUUID=CALL, held='false'),
                 dict(operation='merge', callUUID=CALL, groupWithCallUUID=ACTIVE),
                 dict(operation='unmerge', callUUID=CALL),
                 dict(operation='end-held-and-accept', callUUID=CALL, activeCallUUID=ACTIVE, heldCallUUID=HELD)]
        with patch.dict(send.__globals__, control=control):
            for command in cases:
                with self.subTest(operation=command):
                    self.assertTrue(send(command)['success'])
                    args, device = control.call_args.args
                    self.assertEqual((args.operation, args.call, device), (command['operation'], CALL.lower(), 'device'))
                    self.assertEqual(control.call_args.kwargs['expected_session'], self.session)
                    self.assertIsNone(args.command_receipt)
                    for key, argument in [('activeCallUUID', 'active_call'), ('heldCallUUID', 'held_call'),
                                          ('groupWithCallUUID', 'group_with_call'), ('held', 'held')]:
                        expected = command.get(key)
                        if expected is not None and key != 'held': expected = expected.lower()
                        self.assertEqual(getattr(args, argument, None), expected)

    def test_new_controls_reject_missing_extra_or_ambiguous_targets(self):
        cases = [dict(operation='hold', callUUID=CALL), dict(operation='hold', callUUID=CALL, held=True),
                 dict(operation='hold', callUUID=CALL, held='yes'),
                 dict(operation='merge', callUUID=CALL),
                 dict(operation='merge', callUUID=CALL, groupWithCallUUID='bad'),
                 dict(operation='merge', callUUID=CALL, groupWithCallUUID=ACTIVE, held='true'),
                 dict(operation='unmerge', callUUID=CALL, groupWithCallUUID=ACTIVE),
                 dict(operation='end-held-and-accept', callUUID=CALL, activeCallUUID=ACTIVE),
                 dict(operation='end-held-and-accept', callUUID=CALL, activeCallUUID=ACTIVE, heldCallUUID=ACTIVE),
                 dict(operation='end-held-and-accept', callUUID=CALL, activeCallUUID=CALL, heldCallUUID=HELD),
                 dict(operation='end-held-and-accept', callUUID=CALL, activeCallUUID=ACTIVE, heldCallUUID=23),
                 dict(operation='system-merge', callUUID=CALL, groupWithCallUUID=ACTIVE)]
        control = Mock(side_effect=AssertionError('Invalid controls must not reach the mailbox'))
        with patch.dict(self.bridge.command.__globals__, control=control):
            for command in cases:
                with self.subTest(command=command), self.assertRaises((RuntimeError, ValueError)):
                    self.bridge.command(command)
        control.assert_not_called()

    def test_merge_keeps_app_owned_validation_including_same_call_target(self):
        control = Mock(return_value=dict(success=True))
        with patch.dict(self.bridge.command.__globals__, control=control):
            self.bridge.command(dict(operation='merge', callUUID=CALL, groupWithCallUUID=CALL))
        self.assertEqual(control.call_args.args[0].group_with_call, CALL.lower())

    def test_cli_explicit_flags_reach_the_existing_control_transport(self):
        main = CLI['main']
        cases = [('merge', [CALL, '--group-with-call', ACTIVE], 'group_with_call', ACTIVE),
                 ('unmerge', [CALL], 'call', CALL),
                 ('hold', [CALL, '--held', 'false'], 'held', 'false'),
                 ('end-held-and-accept', [CALL, '--active-call', ACTIVE, '--held-call', HELD], 'held_call', HELD)]
        for operation, flags, field, expected in cases:
            control = Mock(return_value=dict(success=True))
            with self.subTest(operation=operation), patch.dict(main.__globals__, control=control,
                    validate_package=Mock(), simulator=Mock(return_value='device')):
                with patch('sys.argv', ['cksim', '--bundle', 'test.fixture', operation, *flags]), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(), 0)
            self.assertEqual(control.call_args.args[0].operation, operation)
            self.assertEqual(getattr(control.call_args.args[0], field), expected)

    def test_new_fields_publish_to_the_session_pinned_spawn_free_mailbox(self):
        container = self.root / 'container'
        channel = container / 'Documents/.callkit-simulator'
        mailbox = channel / 'run'
        for directory in ('requests', 'responses'):
            (mailbox / directory).mkdir(parents=True)
        session = dict(self.session, controlTransport='directory-watch-v1')
        (channel / 'session.json').write_text(json.dumps(session))
        elapsed, captured = [0.0], []
        seen = set()
        def respond(_):
            elapsed[0] += 0.05
            for path in (mailbox / 'requests').glob('*.json'):
                if path in seen: continue
                seen.add(path)
                request = json.loads(path.read_text())
                captured.append(request)
                (mailbox / 'responses' / path.name).write_text(json.dumps(dict(request, success=True)))
        control = CLI['control']
        rpc = Mock(side_effect=AssertionError('No simulator RPC is needed for directory-watch controls'))
        clock = SimpleNamespace(monotonic=lambda: elapsed[0], sleep=respond)
        cases = [('merge', dict(group_with_call=ACTIVE), dict(groupWithCallUUID=ACTIVE)),
                 ('hold', dict(held='true'), dict(held=True)),
                 ('end-held-and-accept', dict(active_call=ACTIVE, held_call=HELD), dict(activeCallUUID=ACTIVE, heldCallUUID=HELD))]
        with patch.dict(control.__globals__, target_container=Mock(return_value=container), simctl=rpc, time=clock):
            for operation, fields, expected in cases:
                args = SimpleNamespace(bundle='test.fixture', runtime_dir=self.root, app_container=container,
                                       operation=operation, call=CALL, timeout=1, command_receipt=None, **fields)
                self.assertTrue(control(args, 'device', expected_session=session)['success'])
                self.assertEqual(captured[-1]['callUUID'], CALL)
                self.assertEqual({key: captured[-1][key] for key in expected}, expected)
                self.assertEqual({key: captured[-1][key] for key in ('runID', 'pid', 'bundleID')},
                                 {key: session[key] for key in ('runID', 'pid', 'bundleID')})
        rpc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
