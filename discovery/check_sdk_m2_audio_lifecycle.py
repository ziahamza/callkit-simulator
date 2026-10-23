#!/usr/bin/env python3
"""Synthetic audio lifecycle positives/tampers; never claims native delivery."""
import unittest

from sdk_m2_audio_lifecycle import validate_authority, validate_lifecycle
from summarize import EvidenceError

CALL = 'E715ACF2-A7C1-4D74-8BD9-EAFA47693255'


def lifecycle_engine():
    rows = [dict(event='report.incoming', callUUID=CALL),
            dict(event='call.changed', uuid=CALL, outgoing=False, connected=False, held=False, ended=False)]
    kinds = ['CXAnswerCallAction', 'CXSetHeldCallAction', 'CXSetHeldCallAction', 'CXEndCallAction']
    for index, kind in enumerate(kinds):
        action = '00000000-0000-0000-0000-' + str(index + 1).zfill(12)
        rows += [dict(event='transaction.delivered', kinds=[kind], actionIDs=[action]),
                 dict(event='action.settled', actionID=action, fulfilled=True),
                 dict(event='call.changed', uuid=CALL, outgoing=False, connected=True,
                      held=index == 1, ended=index == 3),
                 dict(event='audio.' + ('activated' if index % 2 == 0 else 'deactivated'))]
    return rows


def lifecycle_rows(path='legacy'):
    """Synthetic capture of the produced legacy interruption wiring contract."""
    assert path == 'legacy'
    rows = [dict(event='lifecycle.path', path=path, evidence='native-synthetic-legacy-wiring'),
            dict(event='lifecycle.adapterAuthorization', authorized=False, phase='initial')]
    def add(event, **fields): rows.append(dict(event='lifecycle.' + event, **fields))
    def delegate(phase, active, count):
        add('adapterAuthorization', authorized=active, phase=phase)
        add('lckDelegate', active=active, count=count, phase=phase)
    add('handler', kind='join', phase='answer'); delegate('answer', True, 1)
    for phase, kind, before in [('legacy-began', 'inactive', True), ('legacy-ended', 'recommended', False)]:
        fields = dict(kind=kind, phase=phase, hasSource=False, source=0,
                      hasInterruptionReason=kind == 'inactive', interruptionReason=0, shouldResume=kind == 'recommended')
        add('syntheticPost', kind=kind, phase=phase)
        add('observed', **fields, observerMain=True, notificationName='AVAudioSessionInterruptionNotification', contextClass='')
        if kind == 'inactive': add('adapterAuthorization', authorized=False, phase=phase)
        add('consumed', **fields, authorizedBefore=before, authorizedAfter=False)
    for phase, kind, active, count in [('hold', 'hold', False, 1), ('resume', 'hold', True, 2), ('end', 'end', False, 2)]:
        add('handler', kind=kind, phase=phase); delegate(phase, active, count)
    add('completed', authorized=False, activations=2, deactivations=2, observedCount=2, consumedCount=2, phase='end')
    return rows


class LifecycleControls(unittest.TestCase):
    def validate(self, rows, engine=None):
        return validate_lifecycle(rows, lifecycle_engine() if engine is None else engine, 'legacy')

    def reject(self, change):
        rows, engine = lifecycle_rows(), lifecycle_engine()
        change(rows, engine)
        with self.assertRaises(EvidenceError): self.validate(rows, engine)

    @staticmethod
    def row(rows, event, phase=None):
        return next(row for row in rows if row['event'] == 'lifecycle.' + event and
                    (phase is None or row.get('phase') == phase))

    def test_legacy_wiring_scope(self):
        result = self.validate(lifecycle_rows())
        self.assertEqual(result['observedCount'], 2)
        self.assertFalse(result['systemInterruptionMeasured'])

    def test_scalar_mapping_main_and_types(self):
        for key, value in [('source', 1), ('hasSource', 1), ('hasInterruptionReason', 0),
                           ('interruptionReason', False), ('shouldResume', 0), ('observerMain', 1),
                           ('notificationName', 'AVAudioSessionDidBecomeInactiveNotification'), ('contextClass', 'invalid')]:
            with self.subTest(key=key):
                self.reject(lambda rows, engine, key=key, value=value:
                            self.row(rows, 'observed', 'legacy-began').update({key: value}))

    def test_cancellation_recommendation_and_order(self):
        self.reject(lambda rows, engine: self.row(rows, 'consumed', 'legacy-ended').update(authorizedAfter=True))
        self.reject(lambda rows, engine: rows.remove(self.row(rows, 'syntheticPost', 'legacy-ended')))
        self.reject(lambda rows, engine: rows.remove(self.row(rows, 'adapterAuthorization', 'legacy-began')))
        self.reject(lambda rows, engine: self.row(rows, 'lckDelegate', 'hold').update(count=True))
        self.reject(lambda rows, engine: self.row(rows, 'completed').update(observedCount=True))
        def wrong_reason(rows, engine):
            for event in ('observed', 'consumed'):
                self.row(rows, event, 'legacy-began').update(hasInterruptionReason=False)
        self.reject(wrong_reason)

    def test_native_action_ledger(self):
        self.reject(lambda rows, engine: next(row for row in engine if row['event'] == 'transaction.delivered').update(kinds=['CXStartCallAction']))
        self.reject(lambda rows, engine: next(row for row in engine if row['event'] == 'action.settled').update(fulfilled=1))
        self.reject(lambda rows, engine: engine.remove(next(row for row in engine if row['event'] == 'audio.deactivated')))
        self.reject(lambda rows, engine: next(row for row in engine if row['event'] == 'call.changed').update(uuid='other-call'))


def authority_rows():
    """Synthetic complete LCK authority journey with no notification delivery."""
    rows = [dict(event='lifecycle.path', path='modern', evidence='native-lck-audio-authority-v1'),
            dict(event='lifecycle.adapterAuthorization', authorized=False, phase='initial')]
    for phase, kind, active, count in [('answer', 'join', True, 1), ('hold', 'hold', False, 1),
                                        ('resume', 'hold', True, 2), ('end', 'end', False, 2)]:
        rows += [dict(event='lifecycle.handler', kind=kind, phase=phase),
                 dict(event='lifecycle.adapterAuthorization', authorized=active, phase=phase),
                 dict(event='lifecycle.lckDelegate', active=active, count=count, phase=phase)]
        if phase == 'answer':
            rows += [dict(event='lifecycle.joinSettled', conversationID=CALL, pendingCleared=True,
                          connected=False, phase=phase),
                     dict(event='lifecycle.backendConnectedRequested', conversationID=CALL, phase=phase),
                     dict(event='lifecycle.backendConnected', conversationID=CALL, accepted=True,
                          connectedAtMS=1792627200000, phase=phase)]
    rows += [dict(event='lifecycle.noConversations', count=0, phase='end'),
             dict(event='lifecycle.completed', authorized=False, activations=2, deactivations=2,
                  observedCount=0, consumedCount=0, phase='end')]
    return rows


def passive_pair(phase, kind, before, *, source=None, interruption_reason=None):
    """Modeled scalar transport only; these are never posted as Apple contexts."""
    fields = dict(kind=kind, phase=phase, hasSource=source is not None, source=source or 0,
                  hasInterruptionReason=interruption_reason is not None,
                  interruptionReason=interruption_reason or 0, shouldResume=kind == 'recommended')
    names = dict(active='AVAudioSessionDidBecomeActiveNotification',
                 inactive='AVAudioSessionDidBecomeInactiveNotification',
                 recommended='AVAudioSessionResumptionRecommendationNotification')
    context = 'ModeledDeactivationContext' if kind == 'inactive' and source is not None else (
        'ModeledResumptionContext' if kind == 'recommended' else '')
    values = [dict(event='lifecycle.observed', **fields, observerMain=True,
                   notificationName=names[kind], contextClass=context)]
    if kind == 'inactive':
        values.append(dict(event='lifecycle.adapterAuthorization', authorized=False, phase=phase))
    values.append(dict(event='lifecycle.consumed', **fields, authorizedBefore=before,
                       authorizedAfter=False if kind == 'inactive' else before))
    return values


def authority_rows_with_notifications():
    rows = authority_rows()
    for phase, kind, before, source, reason in [('answer', 'active', False, None, None),
                                               ('hold', 'inactive', True, 0, 1),
                                               ('resume', 'recommended', False, None, None),
                                               ('end', 'inactive', True, 3, None)]:
        at = next(index for index, row in enumerate(rows)
                  if row['event'] == 'lifecycle.adapterAuthorization' and row['phase'] == phase)
        rows[at:at] = passive_pair(phase, kind, before, source=source, interruption_reason=reason)
    count = len([row for row in rows if row['event'] == 'lifecycle.observed'])
    rows[-1].update(observedCount=count, consumedCount=count)
    return rows


class AuthorityControls(unittest.TestCase):
    def validate(self, rows, engine=None):
        return validate_authority(rows, lifecycle_engine() if engine is None else engine)

    def reject(self, change, notifications=False):
        rows = authority_rows_with_notifications() if notifications else authority_rows()
        engine = lifecycle_engine()
        change(rows, engine)
        with self.assertRaises(EvidenceError):
            self.validate(rows, engine)

    @staticmethod
    def row(rows, name, phase=None):
        return LifecycleControls.row(rows, name, phase)

    def test_zero_notification_authority_is_explicitly_unexercised(self):
        result = self.validate(authority_rows())
        self.assertTrue(result['success'])
        self.assertFalse(result['notificationTransportExercised'])
        self.assertEqual((result['observedCount'], result['consumedCount']), (0, 0))
        self.assertFalse(result['systemInterruptionMeasured'])
        self.assertNotIn('notificationTransportPassed', result)

    def test_passive_delivery_preserves_non_app_sources_and_actual_counts(self):
        result = self.validate(authority_rows_with_notifications())
        self.assertTrue(result['notificationTransportExercised'])
        self.assertEqual((result['observedCount'], result['consumedCount']), (4, 4))
        rows = authority_rows()
        at = rows.index(self.row(rows, 'backendConnectedRequested'))
        rows[at:at] = passive_pair('answer', 'active', True)
        rows[-1].update(observedCount=1, consumedCount=1)
        self.assertTrue(self.validate(rows)['success'])

    def test_missing_actions_delegates_or_handler_phases_are_rejected(self):
        for kind in ('CXAnswerCallAction', 'CXSetHeldCallAction', 'CXEndCallAction'):
            with self.subTest(kind=kind):
                self.reject(lambda rows, engine, kind=kind: engine.remove(next(
                    row for row in engine if row['event'] == 'transaction.delivered' and row['kinds'] == [kind])))
        for phase in ('answer', 'hold', 'resume', 'end'):
            for event in ('lckDelegate', 'handler'):
                with self.subTest(phase=phase, event=event):
                    self.reject(lambda rows, engine, phase=phase, event=event:
                                rows.remove(self.row(rows, event, phase)))
        self.reject(lambda rows, engine: engine.remove(next(row for row in engine if row['event'] == 'audio.deactivated')))

    def test_empty_state_final_authority_counts_and_late_events_are_required(self):
        for key, value in [('authorized', True), ('activations', True), ('deactivations', 1),
                           ('observedCount', True), ('consumedCount', 1)]:
            with self.subTest(key=key):
                self.reject(lambda rows, engine, key=key, value=value:
                            self.row(rows, 'completed').update({key: value}))
        self.reject(lambda rows, engine: rows.remove(self.row(rows, 'noConversations')))
        self.reject(lambda rows, engine: self.row(rows, 'noConversations').update(count=1))
        self.reject(lambda rows, engine: self.row(rows, 'noConversations').update(count=False))
        self.reject(lambda rows, engine: rows.append(dict(event='lifecycle.adapterAuthorization', authorized=True, phase='end')))

    def test_lck_authorization_and_order_are_independent_requirements(self):
        for phase in ('answer', 'resume'):
            self.reject(lambda rows, engine, phase=phase: self.row(rows, 'adapterAuthorization', phase).update(authorized=False))
        for phase in ('initial', 'hold', 'end'):
            self.reject(lambda rows, engine, phase=phase: self.row(rows, 'adapterAuthorization', phase).update(authorized=True))
        def reorder(rows, engine):
            delegate = self.row(rows, 'lckDelegate', 'hold')
            rows.remove(delegate)
            rows.insert(rows.index(self.row(rows, 'handler', 'hold')), delegate)
        self.reject(reorder)

    def test_backend_connection_is_bound_after_signaling_before_hold(self):
        for name in ('joinSettled', 'backendConnectedRequested', 'backendConnected'):
            self.reject(lambda rows, engine, name=name: rows.remove(self.row(rows, name)))
            self.reject(lambda rows, engine, name=name: self.row(rows, name).update(conversationID='other-call'))
        for name, key, value in [('joinSettled', 'pendingCleared', False), ('joinSettled', 'connected', True),
                                 ('backendConnected', 'accepted', 1), ('backendConnected', 'connectedAtMS', True)]:
            self.reject(lambda rows, engine, name=name, key=key, value=value:
                        self.row(rows, name).update({key: value}))
        def early(rows, engine):
            requested = self.row(rows, 'backendConnectedRequested')
            rows.remove(requested)
            rows.insert(rows.index(self.row(rows, 'joinSettled')), requested)
        self.reject(early)

    def test_passive_unmatched_mapping_main_and_boolean_coercions_are_rejected(self):
        self.reject(lambda rows, engine: rows.remove(self.row(rows, 'consumed', 'hold')), True)
        self.reject(lambda rows, engine: rows.remove(self.row(rows, 'observed', 'hold')), True)
        for key, value in [('source', 1), ('hasSource', 1), ('hasInterruptionReason', 0),
                           ('interruptionReason', False), ('shouldResume', 0), ('observerMain', 1),
                           ('notificationName', 'AVAudioSessionInterruptionNotification')]:
            self.reject(lambda rows, engine, key=key, value=value:
                        self.row(rows, 'observed', 'hold').update({key: value}), True)
        self.reject(lambda rows, engine: self.row(rows, 'consumed', 'hold').update(authorizedBefore=1), True)
        self.reject(lambda rows, engine: self.row(rows, 'consumed', 'hold').update(authorizedBefore=False), True)

    def test_passive_inactive_revokes_before_consumption_and_advisory_never_grants(self):
        self.reject(lambda rows, engine: self.row(rows, 'consumed', 'hold').update(authorizedAfter=True), True)
        def missing_revocation(rows, engine):
            raw, decoded = self.row(rows, 'observed', 'hold'), self.row(rows, 'consumed', 'hold')
            authorization = next(row for row in rows[rows.index(raw):rows.index(decoded)]
                                 if row['event'] == 'lifecycle.adapterAuthorization')
            rows.remove(authorization)
        self.reject(missing_revocation, True)
        for phase in ('answer', 'resume'):
            self.reject(lambda rows, engine, phase=phase:
                        self.row(rows, 'consumed', phase).update(authorizedAfter=True), True)

    def test_ordinary_session_capture_cannot_be_reclassified_as_lck_authority(self):
        for relabel in (False, True):
            rows = authority_rows()
            if not relabel: self.row(rows, 'path').update(evidence='native-session-lifecycle')
            rows.insert(-1, dict(event='lifecycle.fixtureSessionRequest', active=True, phase='fixture-app-activation'))
            with self.assertRaises(EvidenceError): self.validate(rows)
        with self.assertRaises(EvidenceError): validate_lifecycle(authority_rows(), lifecycle_engine(), 'modern')

    def test_partial_native_journey_stays_rejected(self):
        early = authority_rows()
        early[:] = early[:early.index(self.row(early, 'joinSettled'))]
        with self.assertRaises(EvidenceError): self.validate(early, lifecycle_engine()[:6])


if __name__ == '__main__':
    unittest.main()
