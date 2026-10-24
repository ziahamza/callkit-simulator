import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import run


class RunnerTests(unittest.TestCase):
    def test_reused_fixture_still_launches_a_fresh_pinned_session_without_reinstall(self):
        app = Path('/owned/RuntimeEventFixture.app')
        with patch.object(run.uuid, 'uuid4', return_value='fresh'):
            with patch.object(run, 'run', side_effect=['fixture: 42', '/owned/container']) as transport:
                channel, run_id = run.launch_fixture('owned', app, install=False)
        self.assertEqual(channel, Path('/owned/container/Documents/.callkit-simulator'))
        self.assertEqual(run_id, 'fixture-fresh')
        self.assertEqual([call.args[2] for call in transport.call_args_list], ['launch', 'get_app_container'])
        self.assertIn('--terminate-running-process', transport.call_args_list[0].args)
        self.assertEqual(transport.call_args_list[0].kwargs['env']['SIMCTL_CHILD_CKSIM_RUN_ID'], run_id)

    def test_initial_stimulus_is_sent_after_slow_host_readiness(self):
        """A launch-time ring may already have expired when transport returns."""
        class StimulusSent(Exception):
            pass
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            channel = root / 'Documents/.callkit-simulator'
            (channel / 'session').mkdir(parents=True)
            session = dict(schemaVersion=1, runID='session', pid=123, bundleID=run.BUNDLE, controlTransport='directory-watch-v1')
            (channel / 'session.json').write_text(json.dumps(session))
            # Exactly the state left when the startup ring expires before the
            # host's first observation. The next call must be host-triggered.
            (channel / 'session/events.jsonl').write_text(json.dumps(dict(
                event='call.changed', uuid='old-ring', ended=True)) + '\n')
            ready = []
            def transport(*args, **kwargs):
                if args[:4] == ('xcrun', 'simctl', 'list', 'runtimes'):
                    return json.dumps(dict(runtimes=[dict(identifier='ios-27', version='27.0', platform='iOS', isAvailable=True)]))
                if args[:4] == ('xcrun', 'simctl', 'list', 'devices'):
                    return json.dumps(dict(devices={'ios-27': [dict(udid='owned', state='Booted')]}))
                if args[:3] == ('xcrun', 'simctl', 'launch'):
                    env = kwargs.get('env', {})
                    if env.get('SIMCTL_CHILD_CKSIM_RUN_ID'):
                        session['runID'] = env['SIMCTL_CHILD_CKSIM_RUN_ID']
                        (channel / session['runID']).mkdir()
                        (channel / session['runID'] / 'events.jsonl').write_text('')
                        (channel / 'session.json').write_text(json.dumps(session))
                if args[:3] == ('xcrun', 'simctl', 'get_app_container'):
                    ready.append(True)
                    return str(root)
                if 'push' in args:
                    self.assertTrue(ready, 'push must follow host readiness')
                    raise StimulusSent()
                return ''
            def immediate_wait(predicate):
                value = predicate()
                if not value:
                    raise RuntimeError('Native event acceptance timed out')
                return value
            argv = ['run.py', '--package', str(root), '--app', str(root), '--out', str(root / 'out'),
                    '--simulator', 'owned', '--runtime', 'ios-27']
            with patch.object(run.sys, 'argv', argv), patch.object(run, 'run', side_effect=transport):
                with patch.object(run, 'wait', side_effect=immediate_wait), patch.object(run, 'capture_diagnostics'):
                    with self.assertRaises(StimulusSent):
                        run.main()

    def test_runtime_selection_matches_sdk_numeric_latest_available_ios(self):
        def runtime(version, available=True, platform='iOS'):
            return dict(identifier=version, version=version, name=platform+' '+version,
                        isAvailable=available, platform=platform)
        rows = [runtime('26.2'), runtime('26.5'), runtime('26.10'),
                runtime('27.0', available=False), runtime('28.0', platform='tvOS')]
        self.assertEqual(run.latest_runtime(rows), '26.10')
        with self.assertRaisesRegex(RuntimeError, 'No available iOS'):
            run.latest_runtime(rows[-2:])

    def test_fixture_readiness_rejects_a_previous_launch_session(self):
        with tempfile.TemporaryDirectory() as work:
            channel = Path(work) / 'Documents/.callkit-simulator'
            channel.mkdir(parents=True)
            session_file = channel / 'session.json'
            session_file.write_text(json.dumps(dict(runID='old-launch')))
            def transport(*args, **kwargs):
                if args[:3] == ('xcrun', 'simctl', 'launch'):
                    self.assertEqual(kwargs['env']['SIMCTL_CHILD_CKSIM_RUN_ID'], 'fixture-fresh')
                return str(Path(work))
            def observe(predicate):
                self.assertIsNone(predicate(), 'stale launch must not become ready')
                session_file.write_text('{')
                self.assertIsNone(predicate(), 'partial publication must not become ready')
                session_file.write_text(json.dumps(dict(runID='fixture-fresh', pid=42)))
                return predicate()
            with patch.object(run.uuid, 'uuid4', return_value='fresh'), patch.object(run, 'run', side_effect=transport):
                with patch.object(run, 'wait', side_effect=observe):
                    actual_channel, run_id = run.launch_fixture('owned', Path(work))
                    session = run.fixture_session(actual_channel, run_id)
            self.assertEqual(actual_channel, channel)
            self.assertEqual(session['pid'], 42)

    def test_readiness_timeout_keeps_the_already_discovered_channel(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            channel = root / 'known-channel'
            devices = json.dumps(dict(runtimes=[], devices={'ios-27': [dict(udid='owned', state='Booted')]}))
            argv = ['run.py', '--package', work, '--app', work, '--out', str(root / 'out'), '--simulator', 'owned']
            with patch.object(run.sys, 'argv', argv), patch.object(run, 'run', return_value=devices):
                with patch.object(run, 'latest_runtime', return_value='ios-27'), patch.object(run, 'record_environment'):
                    with patch.object(run, 'launch_fixture', return_value=(channel, 'fresh')):
                        with patch.object(run, 'fixture_session', side_effect=RuntimeError('startup timeout')):
                            with patch.object(run, 'capture_diagnostics') as capture:
                                with self.assertRaisesRegex(RuntimeError, 'startup timeout'):
                                    run.main()
            self.assertEqual(capture.call_args.kwargs['channel'], channel)
            self.assertTrue(capture.call_args.kwargs['failed'])

    def test_cold_boot_timeout_is_explicit_and_keeps_diagnostics(self):
        failure = subprocess.TimeoutExpired(['simctl'], 300, output=b'Waiting for SpringBoard')
        with patch.object(run.subprocess, 'run', side_effect=failure) as process:
            with self.assertRaisesRegex(RuntimeError, 'Waiting for SpringBoard'):
                run.run('xcrun', 'simctl', 'bootstatus', 'owned', '-b', timeout=300)
        self.assertEqual(process.call_args.kwargs['timeout'], 300)

    def test_required_runtime_never_falls_back_to_another_os(self):
        rows = [dict(identifier='ios-27', version='27.0', platform='iOS', isAvailable=True),
                dict(identifier='ios-28', version='28.0', platform='iOS', isAvailable=True)]
        self.assertEqual(run.latest_runtime(rows, 'ios-27'), 'ios-27')
        with self.assertRaisesRegex(RuntimeError, 'ios-26'):
            run.latest_runtime(rows, 'ios-26')
        rows[0]['isAvailable'] = False
        with self.assertRaisesRegex(RuntimeError, 'ios-27'):
            run.latest_runtime(rows, 'ios-27')

    def test_failure_keeps_partial_journal_even_when_log_collection_times_out(self):
        with tempfile.TemporaryDirectory() as work:
            container = Path(work) / 'container'
            channel = container / 'Documents/.callkit-simulator'
            (channel / 'run').mkdir(parents=True)
            (channel / 'session.json').write_text(json.dumps(dict(runID='run')))
            raw = b'{"sequence":1}\n{"partial":'
            (channel / 'run/events.jsonl').write_bytes(raw)
            out = Path(work) / 'diagnostics'
            with patch.object(run, 'run', side_effect=[str(container), '', RuntimeError('log timeout')]) as process:
                run.capture_diagnostics(out, 'owned', failed=True)
            self.assertEqual((out / 'runtime-journal/run/events.jsonl').read_bytes(), raw)
            self.assertEqual(json.loads((out / 'runtime-journal/session.json').read_text())['runID'], 'run')
            self.assertIn('log timeout', (out / 'diagnostic-errors.txt').read_text())
            self.assertTrue(all(call.kwargs['timeout'] == 20 for call in process.call_args_list))

    def test_missing_container_does_not_hide_failure_or_skip_other_diagnostics(self):
        with tempfile.TemporaryDirectory() as work:
            out = Path(work)
            with patch.object(run, 'run', side_effect=[RuntimeError('app never installed'), '', 'fixture log']) as process:
                run.capture_diagnostics(out, 'owned', failed=True)
            self.assertEqual(process.call_count, 3)
            self.assertIn('app never installed', (out / 'diagnostic-errors.txt').read_text())
            self.assertEqual((out / 'simulator.log').read_text(), 'fixture log\n')

    def test_known_channel_is_copied_without_contacting_a_stuck_simulator(self):
        with tempfile.TemporaryDirectory() as work:
            channel = Path(work) / 'channel'
            channel.mkdir()
            (channel / 'session.json').write_text('{}')
            out = Path(work) / 'out'
            with patch.object(run, 'run', side_effect=RuntimeError('simctl stuck')) as process:
                run.capture_diagnostics(out, 'owned', channel=channel, failed=True)
            self.assertEqual((out / 'runtime-journal/session.json').read_text(), '{}')
            self.assertEqual(process.call_count, 2, 'only screenshot and logs use simctl')

    def test_full_disk_cannot_replace_the_journey_failure_or_prevent_cleanup(self):
        for failing_method in ('mkdir', 'write_text'):
            with self.subTest(failing_method=failing_method), tempfile.TemporaryDirectory() as work:
                with patch.object(Path, failing_method, side_effect=OSError('No space left on device')):
                    with patch.object(run, 'run', side_effect=RuntimeError('no container')):
                        with contextlib.redirect_stderr(io.StringIO()) as output:
                            with self.assertRaisesRegex(ValueError, 'journey failed'):
                                try:
                                    raise ValueError('journey failed')
                                finally:
                                    run.capture_diagnostics(Path(work), 'owned', failed=True)
                            self.assertIn('No space left on device', output.getvalue())

    def test_delete_is_attempted_after_shutdown_fails(self):
        with patch.object(run, 'run', side_effect=[RuntimeError('shutdown timed out'), '']) as process:
            with self.assertRaisesRegex(RuntimeError, 'shutdown timed out'):
                run.remove_simulators(['owned'])
        self.assertEqual([call.args[2] for call in process.call_args_list], ['shutdown', 'delete'])
        self.assertTrue(all(call.kwargs['timeout'] == 30 for call in process.call_args_list))

    def test_cleanup_keeps_the_original_failure(self):
        with patch.object(run, 'run', side_effect=RuntimeError('cleanup failed')) as process:
            with contextlib.redirect_stderr(io.StringIO()) as output:
                with self.assertRaisesRegex(ValueError, 'original failure'):
                    try:
                        raise ValueError('original failure')
                    finally:
                        run.remove_simulators(['owned'])
        self.assertEqual(process.call_count, 2)
        self.assertIn('cleanup failed', output.getvalue())


if __name__ == '__main__':
    unittest.main()
