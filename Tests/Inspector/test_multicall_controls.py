import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

CLI = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'Tools/cksim'))
CALL = '00000000-0000-0000-0000-000000000001'
TARGET = '00000000-0000-0000-0000-000000000002'


class MultiCallControls(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.session = dict(schemaVersion=1, runID='run', pid=123, bundleID='test.fixture')
        (self.root / 'session.json').write_text(json.dumps(self.session))
        (self.root / 'run').mkdir()
        (self.root / 'run/events.jsonl').write_text('')
        self.bridge = CLI['InspectorBridge'](SimpleNamespace(bundle='test.fixture'), 'device', self.root)

    def test_controls_preserve_values_targets_and_expected_session(self):
        command = self.bridge.command
        control = Mock(return_value={'success': True})
        with patch.dict(command.__globals__, control=control):
            for operation, parameters in [('hold', dict(held='true')), ('hold', dict(held='false')),
                                          ('merge', dict(groupWithCallUUID=TARGET)), ('unmerge', {})]:
                self.assertTrue(command(dict(operation=operation, callUUID=CALL, **parameters))['success'])
                args, device = control.call_args.args
                self.assertEqual((args.operation, args.call, device), (operation, CALL, 'device'))
                self.assertEqual(args.held, parameters.get('held'))
                self.assertEqual(args.group_with_call, parameters.get('groupWithCallUUID'))
                self.assertEqual(control.call_args.kwargs['expected_session'], self.session)

    def test_malformed_controls_cannot_reach_the_mailbox(self):
        command = self.bridge.command
        control = Mock(side_effect=AssertionError('invalid command reached mailbox'))
        with patch.dict(command.__globals__, control=control):
            for parameters in [dict(operation='merge', callUUID=CALL),
                               dict(operation='merge', callUUID=CALL, groupWithCallUUID='bad'),
                               dict(operation='hold', callUUID=CALL, held=True),
                               dict(operation='unmerge', callUUID=CALL, groupWithCallUUID=TARGET)]:
                with self.assertRaises((RuntimeError, ValueError)):
                    command(parameters)
        control.assert_not_called()

    def test_merge_cli_flag_reaches_the_shipping_control(self):
        main = CLI['main']
        control = Mock(return_value={'success': True})
        with patch.dict(main.__globals__, control=control, validate_package=Mock(), simulator=Mock(return_value='device')):
            with patch('sys.argv', ['cksim', '--bundle', 'test.fixture', 'merge', CALL, '--with-call', TARGET]):
                self.assertEqual(main(), 0)
        self.assertEqual(control.call_args.args[0].group_with_call, TARGET)


if __name__ == '__main__':
    unittest.main()
