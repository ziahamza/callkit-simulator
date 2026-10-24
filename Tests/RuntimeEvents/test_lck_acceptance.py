"""Independent journal-oracle controls; native callbacks supply the real acceptance data."""
import json
from pathlib import Path
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
