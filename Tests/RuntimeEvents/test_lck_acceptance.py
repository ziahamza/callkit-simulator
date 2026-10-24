"""Independent journal-oracle controls; native callbacks supply the real acceptance data."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import lck_acceptance as acceptance


class JournalOracleTests(unittest.TestCase):
    def test_changed_callback_without_matching_manager_snapshot_is_not_acceptance(self):
        rows = [dict(event='lck.fixture.changed', callUUID='A', state='joined'),
                dict(event='lck.fixture.snapshot', conversations=[dict(callUUID='A', state='idle')])]
        self.assertFalse(acceptance.snapshot_matches(rows, {'A': 'joined'}))
        rows[-1]['conversations'][0]['state'] = 'joined'
        self.assertTrue(acceptance.snapshot_matches(rows, {'A': 'joined'}))
        rows[-1]['conversations'] = []
        self.assertTrue(acceptance.snapshot_matches(rows, {'A': 'left'}))

    def test_reader_rejects_a_different_runtime_session(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            journey = acceptance.Journey('owned', root, root / 'out')
            journey.channel.mkdir(parents=True)
            journey.session = dict(runID='expected')
            (journey.channel / 'session.json').write_text(json.dumps(dict(runID='replacement')))
            with self.assertRaisesRegex(AssertionError, 'session changed'):
                journey.rows()


class ContainerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.session = dict(schemaVersion=1, runID='fresh-run', bundleID=acceptance.BUNDLE, pid=123,
                            profile='ios-27.0', controlTransport='directory-watch-v1')

    def candidate(self, **changes):
        container = self.root / str(uuid.uuid4()).upper()
        session = dict(self.session, **changes)
        channel = container / 'Documents/.callkit-simulator'
        journal = channel / session['runID']
        journal.mkdir(parents=True)
        (channel / 'session.json').write_text(json.dumps(session))
        row = dict(session, event='lck.fixture.ready', sequence=1, main=True, t_ms=1, wallTimeMS=1)
        (journal / 'events.jsonl').write_text(json.dumps(row) + '\n')
        return container

    def resolve(self):
        return acceptance.resolve_container(self.root, 'fresh-run', acceptance.BUNDLE, 123)

    def test_stale_session_does_not_resolve_the_initial_container(self):
        self.candidate(runID='stale-run')
        self.assertIsNone(self.resolve())
        actual = self.candidate()
        self.assertEqual(self.resolve(), (actual, self.session))

    def test_wrong_launch_pid_is_rejected(self):
        self.candidate(pid=456)
        with self.assertRaisesRegex(RuntimeError, 'PID'):
            self.resolve()

    def test_journal_identity_must_match_the_pinned_session(self):
        actual = self.candidate()
        journal = actual / 'Documents/.callkit-simulator/fresh-run/events.jsonl'
        row = json.loads(journal.read_text())
        row['pid'] = 456
        journal.write_text(json.dumps(row) + '\n')
        with self.assertRaisesRegex(RuntimeError, 'journal identity'):
            self.resolve()

    def test_ambiguous_matching_containers_are_rejected(self):
        self.candidate()
        self.candidate()
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            self.resolve()

    def test_diagnostics_do_not_follow_symlinked_journal_descendants(self):
        container = self.candidate()
        outside = self.root / 'outside-payload'
        outside.write_text('outside container')
        channel = container / 'Documents/.callkit-simulator'
        (channel / 'escaped-file').symlink_to(outside)
        journey = acceptance.Journey('owned', None, self.root / 'out', applications=self.root)
        journey.name, journey.run_id = 'startup', 'fresh-run'
        journey.save()
        copied = self.root / 'out/startup/unpinned-runtime-journals' / container.name
        self.assertFalse((copied / 'escaped-file').exists())
        self.assertFalse((copied / 'escaped-file').is_symlink())

    def test_unpinned_diagnostics_reject_escaped_session_path(self):
        container = self.candidate()
        session = container / 'Documents/.callkit-simulator/session.json'
        outside = self.root / 'outside-session.json'
        outside.write_text(session.read_text())
        session.unlink()
        session.symlink_to(outside)
        journey = acceptance.Journey('owned', None, self.root / 'out', applications=self.root)
        journey.name, journey.run_id = 'startup', 'fresh-run'
        journey.save()
        self.assertFalse((self.root / 'out/startup/unpinned-runtime-journals').exists())

    def test_borrowed_target_must_belong_to_ios_27_not_merely_have_it_installed(self):
        identifier = str(uuid.uuid4()).upper()
        inventory = dict(devices={'com.apple.CoreSimulator.SimRuntime.iOS-26-5': [
            dict(udid=identifier, state='Booted', dataPath=str(self.root / identifier / 'data'))]})
        with patch.object(acceptance, 'run', return_value=json.dumps(inventory)):
            with self.assertRaisesRegex(RuntimeError, 'iOS 27.0'):
                acceptance.simulator_applications(identifier)

    def test_borrowed_simulator_is_never_created_or_cleaned_even_on_failure(self):
        identifier = str(uuid.uuid4()).upper()
        with patch.object(acceptance, 'create') as create, patch.object(acceptance, 'cleanup') as cleanup:
            with self.assertRaisesRegex(ValueError, 'journey failed'):
                with acceptance.simulator_scope(identifier, self.root) as (device, owned):
                    self.assertEqual(device, identifier)
                    self.assertFalse(owned)
                    raise ValueError('journey failed')
        create.assert_not_called()
        cleanup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
