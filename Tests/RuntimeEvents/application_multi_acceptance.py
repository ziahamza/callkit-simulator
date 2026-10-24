"""Exercise real CXCallController.request callbacks in an existing fixture.

These are simulator contract checks. They establish no reference-device timing,
ordering, connection prerequisite, or capacity behavior.
"""


REQUEST_ERROR_DOMAIN = 'com.apple.CallKit.error.requesttransaction'


def assert_application_completion(row, kind, call_uuid, code):
    """Check the callback emitted by the fixture's actual controller request."""
    assert row['event'] == 'fixture.applicationRequestCompleted', row
    assert row.get('requestID') and row.get('transactionID'), row
    assert row['kind'] == kind and row['callUUID'] == call_uuid, row
    assert row['code'] == code, row
    assert row['domain'] == (REQUEST_ERROR_DOMAIN if code else ''), row
    assert row['main'] is True and row['replyCount'] == 1, row


def assert_application_transaction(events, completion, kind, call_uuid, parameters):
    """Require the accepted request to reach and settle through the application seam."""
    assert_application_completion(completion, kind, call_uuid, 0)
    transactions = [row for row in events if row['event'] == 'transaction.delivered'
                    and row.get('requestID') == completion['transactionID']]
    assert len(transactions) == 1, transactions
    transaction = transactions[0]
    assert transaction['transactionID'] != completion['transactionID'], transaction
    assert transaction['origin'] == 'application', transaction
    assert transaction['callUUIDs'] == [call_uuid], transaction
    assert transaction['parameters'] == [parameters], transaction
    assert len(transaction['actionIDs']) == 1, transaction
    action_id = transaction['actionIDs'][0]
    expected_kind = 'group' if kind in ('merge', 'unmerge') else kind
    for event in ('action.deadline', 'action.settled'):
        actions = [row for row in events if row['event'] == event
                   and row.get('actionID') == action_id]
        assert len(actions) == 1, actions
        action = actions[0]
        assert action['transactionID'] == transaction['transactionID'], action
        assert action['origin'] == 'application' and action['callUUID'] == call_uuid, action
        assert action['kind'] == expected_kind, action
        assert all(action.get(key) == value for key, value in parameters.items()), action
        if event == 'action.settled':
            assert action['fulfilled'] is True, action
    return transaction


def run_application_multi_acceptance(command, rows, wait, *, incoming, application_request):
    """Run on an idle, caller-owned session using real fixture controller calls.

    ``command(operation, **fields)`` returns a full raw mailbox response.
    ``rows()`` supplies current journal rows and ``wait(predicate)`` polls until
    truthy. ``incoming(capabilities=None)`` returns a fresh incoming call UUID;
    an empty capabilities list must produce a call without holding/grouping/
    ungrouping support. ``application_request(kind, call_uuid, **fields)`` invokes
    the fixture and returns its already-awaited application completion row.
    """
    def live_calls():
        projected = {}
        for row in rows():
            if row['event'] == 'call.changed':
                projected[row['uuid']] = row
        return {key: row for key, row in projected.items() if not row['ended']}

    def state(call, **values):
        return wait(lambda: next((row for key, row in live_calls().items() if key == call
                                 and all(row.get(k) == v for k, v in values.items())), None))

    def control(operation, **fields):
        response = command(operation, **fields)
        assert response.get('success') is True, response
        return response['result']

    completions = []

    def requested(kind, call, code=0, **fields):
        before = live_calls()
        callback = application_request(kind, call, **fields)
        assert_application_completion(callback, kind, call, code)
        completions.append(callback)
        if code:
            assert live_calls() == before, 'rejected application request mutated a call'
            assert not any(row['event'] == 'transaction.delivered'
                           and row.get('requestID') == callback['transactionID']
                           for row in rows()), 'rejected application request delivered a transaction'
            return callback
        if kind == 'hold':
            parameters = dict(held=fields['held'])
        else:
            parameters = dict(groupTargetUUID=fields.get('groupWithCallUUID', ''))
        def settled():
            events = rows()
            transaction_ids = {row['transactionID'] for row in events
                               if row['event'] == 'transaction.delivered'
                               and row.get('requestID') == callback['transactionID']}
            return any(row['event'] == 'action.settled'
                       and row.get('transactionID') in transaction_ids for row in events)
        wait(settled)
        assert_application_transaction(rows(), callback, kind, call, parameters)
        return callback

    assert not live_calls(), 'application multi-call acceptance requires an idle fixture session'
    unsupported = incoming(capabilities=[])
    capable, peer = incoming(), incoming()
    for call in (unsupported, capable, peer):
        control('answer', callUUID=call)
        state(call, connected=True)
    assert live_calls()[unsupported]['capabilities'] == [], live_calls()[unsupported]
    for call in (capable, peer):
        assert {'pausing', 'merging', 'unmerging'} <= set(live_calls()[call]['capabilities'])

    requested('hold', unsupported, 6, held=True)
    requested('merge', unsupported, 6, groupWithCallUUID=capable)
    requested('merge', capable, 6, groupWithCallUUID=unsupported)
    requested('unmerge', unsupported, 6)
    requested('merge', capable, 6, groupWithCallUUID=capable)

    for held in (True, False):
        requested('hold', capable, held=held)
        state(capable, held=held)
    target_group = live_calls()[peer]['group']
    unsupported_group = live_calls()[unsupported]['group']
    requested('merge', capable, groupWithCallUUID=peer)
    state(capable, group=target_group)
    assert live_calls()[peer]['group'] == target_group
    assert live_calls()[unsupported]['group'] == unsupported_group
    requested('unmerge', capable)
    wait(lambda: live_calls()[capable]['group'] != target_group)
    assert live_calls()[peer]['group'] == target_group
    assert live_calls()[unsupported]['group'] == unsupported_group

    for callback in completions:
        matching = [row for row in rows() if row['event'] == 'fixture.applicationRequestCompleted'
                    and row.get('requestID') == callback['requestID']]
        assert len(matching) == 1 and matching[0]['replyCount'] == 1, matching
    for call in (unsupported, capable, peer):
        control('end', callUUID=call)
    wait(lambda: not live_calls())
    return dict(success=True, applicationRequests=len(completions),
                deviceMultiCallTiming='unverified', deviceMultiCallOrdering='unverified')
