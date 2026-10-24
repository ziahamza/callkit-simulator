"""Host checks for the public application-request acceptance oracle."""
import unittest

from application_multi_acceptance import (assert_application_completion,
                                          assert_application_transaction)


def completion(code=0):
    return dict(event='fixture.applicationRequestCompleted', requestID='stimulus',
                transactionID='requested', code=code,
                domain='com.apple.CallKit.error.requesttransaction' if code else '',
                main=True, replyCount=1, kind='hold', callUUID='call')


def delivered():
    return [
        dict(event='transaction.delivered', transactionID='delivered', requestID='requested',
             origin='application', actionIDs=['action'], callUUIDs=['call'],
             parameters=[dict(held=True)]),
        dict(event='action.deadline', transactionID='delivered', origin='application',
             actionID='action', callUUID='call', kind='hold', held=True),
        dict(event='action.settled', transactionID='delivered', origin='application',
             actionID='action', callUUID='call', kind='hold', held=True, fulfilled=True),
    ]


class ApplicationRequestOracleTests(unittest.TestCase):
    def test_accepts_documented_error_domain_and_single_main_callback(self):
        assert_application_completion(completion(6), 'hold', 'call', 6)

    def test_rejects_wrong_error_domain(self):
        row = completion(6)
        row['domain'] = 'CKSimRuntimeErrorDomain'
        with self.assertRaises(AssertionError):
            assert_application_completion(row, 'hold', 'call', 6)

    def test_rejects_duplicate_or_wrong_queue_callback(self):
        for field, value in (('replyCount', 2), ('main', False)):
            row = completion()
            row[field] = value
            with self.assertRaises(AssertionError):
                assert_application_completion(row, 'hold', 'call', 0)

    def test_accepts_fresh_transaction_preserving_request_identity(self):
        transaction = assert_application_transaction(delivered(), completion(),
                                                      'hold', 'call', dict(held=True))
        self.assertEqual(transaction['transactionID'], 'delivered')

    def test_rejects_delivered_transaction_without_original_request_link(self):
        rows = delivered()
        rows[0]['requestID'] = 'stimulus'
        with self.assertRaises(AssertionError):
            assert_application_transaction(rows, completion(), 'hold', 'call', dict(held=True))

    def test_rejects_reused_application_transaction_identity(self):
        rows = delivered()
        rows[0]['transactionID'] = 'requested'
        with self.assertRaises(AssertionError):
            assert_application_transaction(rows, completion(), 'hold', 'call', dict(held=True))

    def test_rejects_failed_or_nonapplication_settlement(self):
        for field, value in (('fulfilled', False), ('origin', 'system')):
            rows = delivered()
            rows[-1][field] = value
            with self.assertRaises(AssertionError):
                assert_application_transaction(rows, completion(), 'hold', 'call', dict(held=True))


if __name__ == '__main__':
    unittest.main()
