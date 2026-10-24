"""Multi-call acceptance on a caller-owned, already running fixture session.

This checks simulator contracts only. Multi-call timing and device ordering stay
unverified until a reference iPhone capture establishes those behaviors.
"""
import uuid


def assert_composed_transaction(events, action_ids, first_kind, active, waiting):
    """Require ordered actions in one system-delivered CXTransaction."""
    assert len(action_ids) == 2 and len(set(action_ids)) == 2, action_ids
    delivered = [r for r in events if r['event'] == 'transaction.delivered'
                 and r.get('actionIDs') == action_ids]
    assert len(delivered) == 1, delivered
    transaction = delivered[0]
    assert transaction['origin'] == 'system', transaction
    assert transaction['callUUIDs'] == [active, waiting], transaction
    assert transaction['parameters'] == ([dict(held=True), {}] if first_kind == 'hold' else [{}, {}]), transaction
    actions = sorted((r for r in events if r['event'] == 'action.deadline'
                      and r.get('actionID') in action_ids), key=lambda r: r['sequence'])
    assert [r['actionID'] for r in actions] == action_ids, actions
    assert [r['kind'] for r in actions] == [first_kind, 'answer'], actions
    assert [r['callUUID'] for r in actions] == [active, waiting], actions
    assert all(r['transactionID'] == transaction['transactionID'] and r['origin'] == 'system'
               for r in actions), actions
    if first_kind == 'hold':
        assert actions[0].get('held') is True, actions[0]
    return transaction['transactionID']


def assert_pending_group_actions(events, action_ids):
    """Require an observed group delivery that has not reached settlement."""
    assert action_ids, action_ids
    actions = [row for row in events if row['event'] == 'action.deadline'
               and row.get('actionID') in action_ids]
    assert {row['actionID'] for row in actions} == set(action_ids), actions
    assert all(row['kind'] == 'group' for row in actions), actions
    assert not any(row['event'] in ('action.settled', 'action.cancelled', 'action.timeout')
                   and row.get('actionID') in action_ids for row in events), events


def assert_group_overlap_rejection(events, pending_action_ids, rejected_request_id):
    """Prove rejection occurred before the first group action could settle."""
    rejections = [row for row in events if row['event'] == 'control.rejected'
                  and row.get('requestID') == rejected_request_id]
    assert len(rejections) == 1 and rejections[0]['operation'] == 'merge', rejections
    rejection = rejections[0]
    assert not any(row['event'] == 'transaction.delivered'
                   and row.get('requestID') == rejected_request_id for row in events), events
    settlements = [row for row in events if row['event'] == 'action.settled'
                   and row.get('actionID') in pending_action_ids]
    assert all(rejection['sequence'] < row['sequence'] for row in settlements), settlements


def run_multi_acceptance(command, rows, wait, *, incoming, configure_group_delay=None):
    """Run on an idle session without creating, launching or deleting a simulator.

    ``command(operation, **fields)`` sends a raw mailbox command and returns the
    full response, including failures. ``rows()`` returns the session journal;
    ``wait(predicate)`` polls until the returned value is truthy. ``incoming()``
    asks the host fixture for a new incoming call and returns its UUID. Optional
    ``configure_group_delay(call_uuid, milliseconds)`` returns the already-awaited
    ``fixture.groupDelayConfigured`` acknowledgement and enables overlap coverage.
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

    def accepted(operation, **fields):
        response = command(operation, **fields)
        assert response.get('success') is True, response
        return response['result']

    def rejected(operation, **fields):
        before = live_calls()
        sequence = max((r['sequence'] for r in rows()), default=0)
        response = command(operation, **fields)
        assert response.get('success') is False and response.get('error'), response
        assert live_calls() == before, 'rejected control mutated a call'
        assert not any(r['event'] == 'transaction.delivered' and r['sequence'] > sequence
                       for r in rows()), 'rejected control delivered a transaction'

    def answer():
        call = incoming()
        accepted('answer', callUUID=call)
        row = state(call, connected=True)
        assert {'pausing', 'merging', 'unmerging'} <= set(row['capabilities']), row
        return call

    def merge(call, target, while_pending=None):
        before = live_calls()
        result = accepted('merge', callUUID=call, groupWithCallUUID=target)
        target_group = before[target]['group']
        if while_pending is not None:
            while_pending(result)
        state(call, group=target_group)
        current = live_calls()
        assert all(current[key]['group'] == row['group'] for key, row in before.items()
                   if key != call), 'merge moved a call other than the selected call'
        action_ids = result['actionIDs']
        action = wait(lambda: next((r for r in rows() if r['event'] == 'action.deadline'
                                   and r.get('actionID') in action_ids), None))
        assert action.get('groupTargetUUID') == target, action
        transaction = next(r for r in rows() if r['event'] == 'transaction.delivered'
                           and r.get('actionIDs') == action_ids)
        assert transaction['parameters'] == [dict(groupTargetUUID=target)], transaction
        assert transaction['transactionID'] == result['transactionID'], transaction

    def split(call):
        before = live_calls()
        old_group = before[call]['group']
        result = accepted('unmerge', callUUID=call)
        row = wait(lambda: next((r for key, r in live_calls().items()
                                if key == call and r['group'] != old_group), None))
        assert row['group'] not in {r['group'] for key, r in before.items() if key != call}, row
        assert row['group'] not in live_calls(), 'group identity must be independent of call identity'
        assert all(live_calls()[key]['group'] == value['group'] for key, value in before.items()
                   if key != call), 'unmerge changed another call'
        action = wait(lambda: next((r for r in rows() if r['event'] == 'action.deadline'
                                   and r.get('actionID') in result['actionIDs']), None))
        assert action.get('groupTargetUUID') == '', action

    assert not live_calls(), 'multi-call acceptance requires an idle fixture session'
    status = accepted('status')
    configurations = status['providerConfigurations']
    assert any(c['selected'] and c['valid'] and c['maximumCallGroups'] >= 3
               and c['maximumCallsPerCallGroup'] >= 3 for c in configurations), configurations
    first, second, third = answer(), answer(), answer()
    initial = live_calls()
    assert len({r['group'] for r in initial.values()}) == 3, initial
    assert not set(initial) & {r['group'] for r in initial.values()}, initial

    if configure_group_delay is None:
        merge(second, first)
    else:
        def group_delay(milliseconds):
            acknowledgement = configure_group_delay(second, milliseconds)
            assert acknowledgement['event'] == 'fixture.groupDelayConfigured', acknowledgement
            assert acknowledgement.get('requestID'), acknowledgement
            assert acknowledgement['groupDelayMS'] == milliseconds, acknowledgement

        def reject_overlap(result):
            assert_pending_group_actions(rows(), result['actionIDs'])
            before = live_calls()
            response = command('merge', callUUID=third, groupWithCallUUID=first)
            assert response.get('success') is False and response.get('error'), response
            assert_group_overlap_rejection(rows(), result['actionIDs'], response['id'])
            current = live_calls()
            # The accepted first action may settle during the rejected command's
            # transport. Only its selected call may legitimately change here.
            assert all(current[key] == row for key, row in before.items() if key != second), (
                'rejected overlapping control mutated an unrelated call')
            wait(lambda: any(row['event'] == 'action.settled' and row.get('fulfilled') is True
                             and row.get('actionID') in result['actionIDs'] for row in rows()))

        group_delay(1000)
        merge(second, first, while_pending=reject_overlap)
        group_delay(120)
    merge(third, first)
    shared = live_calls()[second]['group']
    split(first)  # The original target must detach from the remaining pair.
    assert live_calls()[second]['group'] == live_calls()[third]['group'] == shared
    split(second)  # Detach a member whose call UUID never named the group.
    assert len({r['group'] for r in live_calls().values()}) == 3
    merge(second, first)
    merge(first, third)  # Moving one member must leave its old peer behind.
    assert live_calls()[second]['group'] != live_calls()[first]['group']

    for held in (True, False):
        result = accepted('hold', callUUID=second, held=held)
        state(second, held=held)
        action = wait(lambda: next((r for r in rows() if r['event'] == 'action.deadline'
                                   and r.get('actionID') in result['actionIDs']), None))
        assert action.get('held') is held, action

    rejected('merge', callUUID=second, groupWithCallUUID=second)
    rejected('merge', callUUID=second)
    rejected('merge', callUUID=second, groupWithCallUUID=str(uuid.uuid4()))
    rejected('unmerge', callUUID=str(uuid.uuid4()))
    stale = incoming()
    accepted('end', callUUID=stale)
    wait(lambda: stale not in live_calls())
    rejected('merge', callUUID=second, groupWithCallUUID=stale)
    ringing = incoming()
    state(ringing, connected=False)
    rejected('merge', callUUID=second, groupWithCallUUID=ringing)
    accepted('end', callUUID=ringing)
    wait(lambda: ringing not in live_calls())
    for call in (first, second, third):
        accepted('end', callUUID=call)
    wait(lambda: not live_calls())

    active = answer()
    transaction_ids = []
    for operation, first_kind in (('hold-and-accept', 'hold'), ('end-and-accept', 'end')):
        waiting = incoming()
        result = accepted(operation, callUUID=waiting, activeCallUUID=active)
        state(waiting, connected=True)
        if first_kind == 'hold':
            state(active, held=True)
        else:
            wait(lambda: active not in live_calls())
        transaction_id = assert_composed_transaction(rows(), result['actionIDs'],
                                                      first_kind, active, waiting)
        assert transaction_id == result['transactionID'], result
        transaction_ids.append(transaction_id)
        active = waiting
    assert len(set(transaction_ids)) == 2, transaction_ids
    for call in list(live_calls()):
        accepted('end', callUUID=call)
    wait(lambda: not live_calls())
    return dict(success=True, composedTransactionIDs=transaction_ids,
                overlapCoverage=configure_group_delay is not None,
                deviceMultiCallTiming='unverified', deviceMultiCallOrdering='unverified')
