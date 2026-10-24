"""Host checks that the native multi-call oracle rejects known regressions."""
import unittest

from multi_acceptance import (assert_composed_transaction, assert_group_overlap_rejection,
                              assert_pending_group_actions)


def composed_rows():
    return [
        dict(event='transaction.delivered', sequence=1, transactionID='shared',
             actionIDs=['hold-action', 'answer-action'], callUUIDs=['active', 'waiting'],
             parameters=[dict(held=True), {}], origin='system'),
        dict(event='action.deadline', sequence=2, transactionID='shared',
             actionID='hold-action', callUUID='active', kind='hold', held=True, origin='system'),
        dict(event='action.deadline', sequence=3, transactionID='shared',
             actionID='answer-action', callUUID='waiting', kind='answer', origin='system'),
    ]


class ComposedTransactionOracleTests(unittest.TestCase):
    def check_rows(self, rows):
        return assert_composed_transaction(rows, ['hold-action', 'answer-action'],
                                           'hold', 'active', 'waiting')

    def test_accepts_one_ordered_system_transaction(self):
        self.assertEqual(self.check_rows(composed_rows()), 'shared')

    def test_rejects_independent_transaction_ids_on_actions(self):
        rows = composed_rows()
        rows[2]['transactionID'] = 'second'
        with self.assertRaises(AssertionError):
            self.check_rows(rows)

    def test_rejects_reversed_action_delivery(self):
        rows = composed_rows()
        rows[1]['sequence'], rows[2]['sequence'] = 3, 2
        with self.assertRaises(AssertionError):
            self.check_rows(rows)

    def test_rejects_answer_first_transaction(self):
        rows = composed_rows()
        rows[0]['actionIDs'].reverse()
        rows[0]['callUUIDs'].reverse()
        rows[0]['parameters'].reverse()
        with self.assertRaises(AssertionError):
            self.check_rows(rows)

    def test_rejects_missing_hold_parameter(self):
        rows = composed_rows()
        del rows[1]['held']
        with self.assertRaises(AssertionError):
            self.check_rows(rows)


class PendingGroupOracleTests(unittest.TestCase):
    def pending(self):
        return [dict(event='action.deadline', sequence=1, actionID='pending', kind='group')]

    def test_accepts_observed_pending_group_action(self):
        assert_pending_group_actions(self.pending(), ['pending'])

    def test_rejects_already_settled_overlap_stimulus(self):
        rows = self.pending() + [dict(event='action.settled', sequence=2, actionID='pending')]
        with self.assertRaises(AssertionError):
            assert_pending_group_actions(rows, ['pending'])

    def test_accepts_first_action_settling_after_overlap_rejection(self):
        rows = self.pending() + [
            dict(event='control.rejected', sequence=2, requestID='rejected', operation='merge'),
            dict(event='action.settled', sequence=3, actionID='pending'),
        ]
        assert_group_overlap_rejection(rows, ['pending'], 'rejected')

    def test_rejects_overlap_sent_after_first_settlement(self):
        rows = self.pending() + [
            dict(event='action.settled', sequence=2, actionID='pending'),
            dict(event='control.rejected', sequence=3, requestID='rejected', operation='merge'),
        ]
        with self.assertRaises(AssertionError):
            assert_group_overlap_rejection(rows, ['pending'], 'rejected')

    def test_rejects_transaction_for_rejected_overlap(self):
        rows = self.pending() + [
            dict(event='control.rejected', sequence=2, requestID='rejected', operation='merge'),
            dict(event='transaction.delivered', sequence=3, requestID='rejected'),
        ]
        with self.assertRaises(AssertionError):
            assert_group_overlap_rejection(rows, ['pending'], 'rejected')


if __name__ == '__main__':
    unittest.main()
