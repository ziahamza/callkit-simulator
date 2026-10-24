"""Independent journal-oracle controls; native callbacks supply the real acceptance data."""
import json
from pathlib import Path
import tempfile
import unittest

import lck_acceptance as acceptance


class JournalOracleTests(unittest.TestCase):
    def test_barrier_rejects_join_before_last_constituent_settlement(self):
        rows = [dict(event='lck.fixture.settled', actionID='join', t_ms=10),
                dict(event='lck.fixture.changed', callUUID='B', state='joined', t_ms=11),
                dict(event='lck.fixture.settled', actionID='pause', t_ms=2000)]
        with self.assertRaisesRegex(AssertionError, 'before every constituent'):
            acceptance.verify_barrier(rows, 'B', {'join', 'pause'})
        rows[1]['t_ms'] = 2010
        acceptance.verify_barrier([rows[0], rows[2], rows[1]], 'B', {'join', 'pause'})
        rows[1]['t_ms'] = rows[2]['t_ms']
        with self.assertRaisesRegex(AssertionError, 'before every constituent'):
            acceptance.verify_barrier(rows, 'B', {'join', 'pause'})

    def test_timeout_checks_effective_elapsed_and_advertised_deadline_separately(self):
        delivered = dict(t_ms=1000, advertisedRemainingMS=30000)
        acceptance.verify_timeout(delivered, dict(t_ms=6330), 'Pause')
        acceptance.verify_timeout(delivered, dict(t_ms=65000), 'Join')
        with self.assertRaises(AssertionError):
            acceptance.verify_timeout(delivered, dict(t_ms=31000), 'Pause')

    def test_changed_callback_without_matching_manager_snapshot_is_not_acceptance(self):
        rows = [dict(event='lck.fixture.changed', callUUID='A', state='joined'),
                dict(event='lck.fixture.snapshot', conversations=[dict(callUUID='A', state='idle')])]
        self.assertFalse(acceptance.snapshot_matches(rows, {'A': 'joined'}))
        rows[-1]['conversations'][0]['state'] = 'joined'
        self.assertTrue(acceptance.snapshot_matches(rows, {'A': 'joined'}))
        rows[-1]['conversations'] = []
        self.assertTrue(acceptance.snapshot_matches(rows, {'A': 'left'}))

    def test_merge_oracle_rejects_transient_state_change_even_after_restoration(self):
        rows = [dict(event='lck.fixture.changed', callUUID='A', state='joined'),
                dict(event='lck.fixture.changed', callUUID='A', state='paused')]
        with self.assertRaisesRegex(AssertionError, 'changed native'):
            acceptance.verify_unchanged(rows, {'A': 'paused'})
        with self.assertRaisesRegex(AssertionError, 'manager snapshot'):
            acceptance.verify_unchanged([dict(event='lck.fixture.snapshot', conversations=[])], {'A': 'paused'})

    def test_reader_rejects_a_different_runtime_session(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            journey = acceptance.Journey('owned', root, root / 'out')
            journey.channel.mkdir(parents=True)
            journey.session = dict(runID='expected')
            (journey.channel / 'session.json').write_text(json.dumps(dict(runID='replacement')))
            with self.assertRaisesRegex(AssertionError, 'session changed'):
                journey.rows()


if __name__ == '__main__':
    unittest.main()
