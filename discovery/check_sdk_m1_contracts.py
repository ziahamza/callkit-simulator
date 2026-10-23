#!/usr/bin/env python3
"""Focused positive/tamper fixtures, with no simulator or native process use."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sdk_m1_contracts_conformance as gate
from check_sdk_m2_audio_lifecycle import authority_rows, lifecycle_engine

IDENTITY = dict(sourceCommit='a' * 40, sourceInputsSHA256='b' * 64, sourceDirty=False)
CALL = '61B0C5C2-78DD-48B8-9C56-0C6A6D53B78A'
ACTION = '03443A64-7BDD-4D77-B91B-048093F9AF04'
WALL = 1792627200000


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def reindex(engine, reset_clock=False):
    # Keep the outer journal valid when a semantic tamper removes/reorders rows.
    for index, row in enumerate(engine):
        row['sequence'] = index + 1
        if reset_clock:
            row['t_ms'] = index
            if 'wallTimeMS' in row: row['wallTimeMS'] = WALL + index


def fixture(build, capture):
    for name in gate.BUILD_FILES:
        path = build / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic fixture, never executable\n')
    for name in ('source-before.json', 'source-after.json'): write(build / name, IDENTITY)
    write(build / 'build-info.json', dict(schemaVersion=1, product='sdk-m1-contracts', configuration='Debug',
          **IDENTITY, files={name: gate.digest(build / name) for name in gate.BUILD_FILES}))
    for index, case in enumerate(gate.CASES):
        pid = 123 + index
        rows = []
        clock = 0
        def add(event, at=None, **fields):
            nonlocal clock
            clock = max(clock + 1, at or 0)
            row = dict(event=event, sequence=len(rows) + 1, t_ms=clock, wallTimeMS=WALL + clock,
                       runID='synthetic-run', simulatorID='synthetic-simulator', pid=pid,
                       bundleID=gate.BUNDLE, main=True, **fields)
            rows.append(row)
            return row
        add('app.entry')
        add('launch', traceVersion=2, mode='sdk-m1-contracts', case=case, runtimeInjectionPresent=False,
            executableSHA256=gate.digest(build / gate.BUILD_FILES[0]), simulatorOS='27.0')
        add('contract.activation', active=True)
        if case in ('unhandled-accept', 'unhandled-fail'):
            add('contract.configuration', policy=case.removeprefix('unhandled-'), handlerAttached=False)
        add('contract.owner', identity='client', sharedIdentity='client', warnings=0)
        if case == 'duplicate-wrapper': rows[-1]['entryPoint'] = 'VoIPApplication.start'
        if case == 'guard':
            for phase, count, identity in [('extra-cx', 1, 'provider-2'), ('extra-lck', 2, 'manager-2')]:
                add('contract.creationBegin', phase=phase)
                add('contract.warning', phase=phase, count=count, message=gate.WARNING)
                add('contract.creationEnd', phase=phase, warnings=count, identity=identity)
        elif case == 'media-reentrancy':
            add('contract.mediaReentrancy', authorizationCalls=1, sameOwner=True, warnings=0)
        elif case == 'audio-lifecycle':
            for row in authority_rows():
                add(row['event'], **{key: value for key, value in row.items() if key != 'event'})
        elif case == 'audio-routes':
            add('routes.lckDelegate', active=True)
            add('routes.authority', conversationID=CALL, authorized=True)
            add('routes.command', id='speaker', succeeded=True)
            snapshot = dict(revision=2, routes=[dict(id='input:mic', name='Microphone', type='MicrophoneBuiltIn'),
                dict(id='speaker', name='Speaker', type='Speaker'),
                dict(id='output:receiver', name='Actual receiver', type='Receiver')],
                current=dict(id='output:receiver', name='Actual receiver', type='Receiver'))
            fields = dict(snapshot=snapshot, inputs=[dict(uid='mic', name='Microphone', type='MicrophoneBuiltIn')],
                          outputs=[dict(uid='receiver', name='Actual receiver', type='Receiver')])
            add('routes.observed', phase='after', **fields)
            add('routes.currentNoOp', available=True, unchanged=True)
            add('routes.observed', phase='unchanged', **fields)
            add('routes.lckDelegate', active=False)
            add('routes.ended', conversationID=CALL, empty=True, authorized=False)
        elif case == 'duplicate-wrapper':
            payload_hash = 'd' * 64
            add('contract.duplicateSnapshot', revision=0, conversations=[])
            for ordinal, source in ((1, 'foreground'), (2, 'pushKit')):
                fields = dict(ordinal=ordinal, source=source, payloadSHA256=payload_hash)
                add('contract.duplicateReportRequested', **fields)
                if ordinal == 1:
                    add('contract.duplicateSnapshot', revision=1,
                        conversations=[dict(id=CALL, status='ringing', hasConnectedAt=False)])
                add('contract.duplicateReportResult', **fields, accepted=True, conversationID=CALL,
                    conversations=[dict(id=CALL, status='ringing', hasConnectedAt=False, hasPending=False)])
            add('contract.duplicateEndRequested', conversationID=CALL)
            add('contract.duplicateEndCommandResult', conversationID=CALL, accepted=True)
            add('contract.duplicateSnapshot', revision=2, conversations=[dict(id=CALL, status='ending',
                hasConnectedAt=False, pending=dict(id=ACTION, kind='end', origin='app'))])
            add('contract.duplicateEndHandler', conversationID=CALL, actionID=ACTION, origin='app')
            add('contract.duplicateSnapshot', revision=3, conversations=[dict(id=CALL, status='ended', hasConnectedAt=False)])
            add('contract.duplicateSnapshot', revision=4, conversations=[])
            add('contract.duplicateObservation', conversationID=CALL, empty=True)
        else:
            pending = dict(id=ACTION, kind='join', origin='app', deadlineMS=WALL + 28_500)
            def snapshot(status=None, pending=None, at=None, connected=False):
                calls = [] if status is None else [dict(id=CALL, status=status, hasConnectedAt=connected)]
                if pending: calls[0]['pending'] = pending
                add('contract.snapshot', at=at, revision=len([r for r in rows if r['event'] == 'contract.snapshot']), conversations=calls)
            snapshot()
            add('contract.incoming', conversationID=CALL)
            snapshot('ringing', pending=pending)
            sample = add('contract.pending', conversationID=CALL, actionID=ACTION, kind='join', origin='app',
                         deadlineMS=WALL + 28_500, remainingMS=0, handlerAttached=False, clockBeforeMS=WALL + clock + 1)
            sample['remainingMS'] = sample['deadlineMS'] - sample['wallTimeMS']
            add('contract.joinResult', accepted=True)
            if case == 'unhandled-accept':
                snapshot('connecting', at=28_501)
                add('contract.joinSettled', joined=True, conversationID=CALL)
                add('contract.backendConnectedRequested', conversationID=CALL)
                snapshot('connected', connected=True)
                add('contract.backendConnected', accepted=True, conversationID=CALL)
                add('contract.observation', connected=True, conversationID=CALL)
                add('contract.endReportRequested', conversationID=CALL)
                snapshot()
                add('contract.reportEnded', conversationID=CALL)
            else:
                def diagnostic(phase, at, entry, platform):
                    row = add('contract.actionDiagnostic', at=at, phase=phase, actionID=ACTION,
                              sdkWallTimeMS=WALL + at, effectiveDeadlineMS=pending['deadlineMS'],
                              platformDeadlineMS=WALL + 30_000, entryPending=entry, platformPending=platform)
                    return row
                diagnostic('policy-fire', 28_500, True, True)
                diagnostic('platform-settle-enter', 28_501, False, True)
                diagnostic('platform-settle-return', 28_502, False, False)
                snapshot('ringing', at=28_503)
                diagnostic('pending-state-published', 28_504, False, False)
                add('contract.failureObservation', at=30_001, conversationID=CALL, pendingCleared=True,
                    conversationPresent=True, status='ringing')
                add('contract.fixtureCleanupRequested', conversationID=CALL, reason='failed')
                snapshot('ended')
                add('contract.fixtureCleanupReported', conversationID=CALL, reason='failed')
                snapshot()
        add('contract.result', success=True); add('done')
        engine = []
        def runtime(event, at=None, **fields):
            engine.append(dict(event=event, sequence=len(engine) + 1, t_ms=len(engine) if at is None else at,
                runID='synthetic-run', pid=pid, bundleID=gate.BUNDLE, profile='ios-27.0', **fields))
        runtime('runtime.installed', version=(gate.ROOT / 'VERSION').read_text().strip())
        for count in range(1, 4 if case == 'guard' else 2): runtime('provider.created', provider='provider-' + str(count))
        if case in ('unhandled-accept', 'unhandled-fail'):
            runtime('transaction.delivered', kinds=['CXAnswerCallAction'], actionIDs=['native-cx-action'],
                    provider='provider-1', origin='application', requestID='native-request')
            runtime('action.deadline', actionID='native-cx-action', apiKind='livecommunicationkit',
                    provider='provider-1', deadlineMS=WALL + 30_000, deadlineSeconds=30, expirySeconds=30)
            runtime('contract.pendingClock', at=sample['t_ms'], actionID=ACTION)
            if case == 'unhandled-accept':
                runtime('action.settled', at=28_500, actionID='native-cx-action', fulfilled=True)
                runtime('call.changed', at=28_501, uuid=CALL, connected=True, ended=False)
                runtime('report.ended', at=28_510, callUUID=CALL)
                runtime('call.changed', at=28_511, uuid=CALL, connected=True, ended=True)
            else:
                runtime('action.settled', at=28_501, actionID='native-cx-action', fulfilled=False, origin='application')
                runtime('report.ended', at=30_002, callUUID=CALL, reason=1)
                runtime('call.changed', at=30_003, uuid=CALL, connected=False, ended=True)
        elif case == 'audio-lifecycle':
            for row in lifecycle_engine():
                runtime(row['event'], **{key: value for key, value in row.items() if key != 'event'})
        elif case == 'audio-routes':
            for kind, action, ended, audio in [('CXAnswerCallAction', 'join', False, 'activated'),
                                                ('CXEndCallAction', 'end', True, 'deactivated')]:
                runtime('transaction.delivered', kinds=[kind], actionIDs=[action])
                runtime('action.settled', actionID=action, fulfilled=True)
                runtime('call.changed', uuid=CALL, connected=False, ended=ended)
                runtime('audio.' + audio)
        elif case == 'duplicate-wrapper':
            runtime('provider.apiKind', provider='provider-1', apiKind='livecommunicationkit', delegatePresent=True)
            for ordinal, source in ((1, 'foreground'), (2, 'pushKit')):
                fields = dict(ordinal=ordinal, source=source, payloadSHA256=payload_hash)
                runtime('contract.duplicateReportBoundary', **fields, stage='requested')
                runtime('report.incoming', callUUID=CALL, pushID='')
                if ordinal == 1: runtime('call.changed', uuid=CALL, outgoing=False, connected=False, ended=False)
                runtime('report.completed', callUUID=CALL, pushID='', error=0 if ordinal == 1 else 2)
                runtime('contract.duplicateReportBoundary', **fields, stage='returned', callUUID=CALL)
            runtime('contract.duplicateEndBoundary', stage='requested', callUUID=CALL)
            runtime('contract.duplicateEndBoundary', stage='command-returned', callUUID=CALL)
            runtime('transaction.delivered', origin='application', provider='provider-1', kinds=['CXEndCallAction'], actionIDs=['native-cx-end'])
            runtime('action.settled', actionID='native-cx-end', fulfilled=True)
            runtime('call.changed', uuid=CALL, outgoing=False, connected=False, ended=True)
            runtime('contract.duplicateEndBoundary', stage='observed-complete', callUUID=CALL, empty=True)
        save(capture, case, rows, engine)


def save(capture, case, rows, engine):
    name = 'sdk-m1-contract-' + case
    log = capture / (name + '.log')
    log.write_text(''.join('PROBE ' + json.dumps(row) + '\n' for row in rows))
    journal = capture / (name + '.runtime.jsonl')
    journal.write_text(''.join(json.dumps(row) + '\n' for row in engine))
    write(capture / (name + '.receipt.json'), dict(schemaVersion=2, scenario=name, bundleID=gate.BUNDLE,
        runID='synthetic-run', simulatorID='synthetic-simulator', completed=True, success=True,
        launcherPID=122, appPID=rows[0]['pid'], launcherAppPID=rows[0]['pid'], appEntryObserved=True, lastProbeEvent='done',
        launchExitCode=0, terminationMethod='console-launcher-signal', terminationSignal=15,
        terminationSignalDelivered=True, consoleTransport='host-pty', appAbsenceMethod='signal-zero',
        appAbsentAfterStop=True, traceSHA256=gate.digest(log), journalSHA256=gate.digest(journal)))


def renumber(*streams):
    for stream in streams:
        for index, row in enumerate(stream): row['sequence'] = index + 1


def later_system_end(rows, engine, settle=False):
    template = engine[0]
    def event(name, at, **fields):
        return {**template, 'event': name, 't_ms': at, **fields}
    index = next(i for i, r in enumerate(engine) if r['event'] == 'action.settled') + 1
    engine[index:index] = [
        event('transaction.delivered', 28_502, kinds=['CXEndCallAction'], actionIDs=['native-system-end'],
              provider='provider-1', origin='system', requestID='system'),
        event('action.deadline', 28_503, actionID='native-system-end', apiKind='livecommunicationkit',
              provider='provider-1', deadlineMS=WALL + 58_502, deadlineSeconds=30, expirySeconds=30)]
    if settle:
        engine.insert(index + 2, event('action.settled', 28_504, actionID='native-system-end', fulfilled=False, origin='system'))
    renumber(engine)


class Controls(unittest.TestCase):
    def test_single_case_selection_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            build, capture = Path(temporary) / 'build', Path(temporary) / 'capture'
            build.mkdir(); capture.mkdir(); fixture(build, capture)
            with patch.object(gate, 'source_identity', return_value=IDENTITY):
                result = gate.validate(build, capture, cases=('audio-lifecycle',))
                self.assertEqual(set(result['cases']), {'audio-lifecycle'})
                with self.assertRaises(gate.EvidenceError): gate.validate(build, capture, cases=())
                with self.assertRaises(gate.EvidenceError): gate.validate(build, capture, cases=('absent',))
                with self.assertRaises(gate.EvidenceError): gate.validate(build, capture, cases=('guard', 'guard'))

    def test_route_observations_preserve_actual_output_and_reject_optimistic_state(self):
        for mutate in (
            lambda rows, engine: next(r for r in rows if r['event'] == 'routes.observed')['snapshot']['current'].update(id='input:mic'),
            lambda rows, engine: next(r for r in rows if r['event'] == 'routes.observed')['snapshot']['current'].update(type='MicrophoneBuiltIn'),
            lambda rows, engine: next(r for r in rows if r['event'] == 'routes.observed')['snapshot']['routes'].append(
                dict(id='output:invented', name='Receiver', type='Receiver')),
            lambda rows, engine: next(r for r in rows if r['event'] == 'routes.observed')['snapshot'].pop('current'),
            lambda rows, engine: next(r for r in rows if r['event'] == 'routes.currentNoOp').update(unchanged=False),
            lambda rows, engine: next(r for r in rows if r['event'] == 'routes.ended').update(authorized=True),
            lambda rows, engine: next(r for r in engine if r['event'] == 'call.changed').update(uuid=ACTION),
            lambda rows, engine: engine.remove(next(r for r in engine if r['event'] == 'audio.activated')),
        ):
            self.check('audio-routes', mutate)
        def setter_error(rows, engine):
            next(r for r in rows if r['event'] == 'routes.command').update(
                succeeded=False, errorDomain='NSOSStatusErrorDomain', errorCode=-50)
        self.check('audio-routes', setter_error, succeeds=True)
        def no_output(rows, engine):
            for row in rows:
                if row['event'] == 'routes.observed':
                    row.update(inputs=[], outputs=[], snapshot=dict(revision=1,
                        routes=[dict(id='speaker', name='Speaker', type='Speaker')], current=None))
                elif row['event'] == 'routes.currentNoOp': row['available'] = False
        self.check('audio-routes', no_output, succeeds=True)

    def test_lifecycle_capture_cannot_relabel_the_runtime_path(self):
        self.check('audio-lifecycle', lambda rows, engine:
            next(row for row in rows if row['event'] == 'launch').update(simulatorOS='26.5'))

    def test_media_adapter_publication_and_reentrancy(self):
        for field, value in [('authorizationCalls', 2), ('authorizationCalls', True),
                             ('sameOwner', False), ('warnings', True), ('warnings', 1)]:
            with self.subTest(field=field, value=value):
                self.check('media-reentrancy', lambda rows, engine, field=field, value=value:
                    next(r for r in rows if r['event'] == 'contract.mediaReentrancy').update({field: value}))
        def extra_provider(rows, engine):
            engine.append({**engine[-1], 'event': 'provider.created', 'provider': 'provider-2'})
            reindex(engine)
        self.check('media-reentrancy', extra_provider)

    def test_accept_cannot_race_the_platform_deadline(self):
        self.check('unhandled-accept', lambda rows, engine:
            next(row for row in engine if row['event'] == 'action.deadline').update(deadlineMS=WALL + 28_500))

    def test_duplicate_command_return_is_not_settlement(self):
        def returned_later(rows, engine):
            result = next(r for r in rows if r['event'] == 'contract.duplicateEndCommandResult')
            rows.remove(result); rows.insert(rows.index(next(r for r in rows if r['event'] == 'contract.duplicateObservation')), result)
            marker = next(r for r in engine if r['event'] == 'contract.duplicateEndBoundary' and r['stage'] == 'command-returned')
            engine.remove(marker); engine.insert(-1, marker)
            reindex(rows, reset_clock=True); reindex(engine, reset_clock=True)
        # The default fixture returns before perform, as measured on device.
        # A slower request completion is valid too; neither return proves settlement.
        self.check('duplicate-wrapper', returned_later, succeeds=True)

    def test_duplicate_report_identity_and_result(self):
        self.check('duplicate-wrapper', lambda rows, engine:
            next(r for r in rows if r['event'] == 'contract.owner').update(entryPoint='ConversationClient.shared'))
        for event, field, value in (
                ('contract.duplicateReportRequested', 'source', 'foreground-only'),
                ('contract.duplicateReportRequested', 'ordinal', True),
                ('contract.duplicateReportRequested', 'payloadSHA256', 'e' * 64),
                ('contract.duplicateReportResult', 'conversationID', 'other-call'),
                ('contract.duplicateReportResult', 'accepted', False)):
            with self.subTest(event=event, field=field):
                self.check('duplicate-wrapper', lambda rows, engine:
                    [r for r in rows if r['event'] == event][-1].update({field: value}))
        for field, value in (('hasConnectedAt', True), ('hasConnectedAt', 0), ('hasPending', True), ('hasPending', 0)):
            with self.subTest(field=field, value=value):
                self.check('duplicate-wrapper', lambda rows, engine:
                    next(r for r in rows if r['event'] == 'contract.duplicateReportResult')['conversations'][0].update({field: value}))
        self.check('duplicate-wrapper', lambda rows, engine:
            next(r for r in rows if r['event'] == 'contract.duplicateReportResult')['conversations'].append(
                dict(id='other-call', status='ringing', hasConnectedAt=False, hasPending=False)))

    def test_duplicate_native_report_pairing(self):
        for event, field, value in (('report.completed', 'error', 0), ('report.completed', 'error', True),
                                   ('report.completed', 'callUUID', 'other-call'), ('report.incoming', 'callUUID', 'other-call'),
                                   ('contract.duplicateReportBoundary', 'payloadSHA256', 'e' * 64)):
            with self.subTest(event=event, field=field, value=value):
                self.check('duplicate-wrapper', lambda rows, engine:
                    [r for r in engine if r['event'] == event][-1].update({field: value}))
        def missing(rows, engine):
            for event in ('report.incoming', 'report.completed'):
                engine.remove([r for r in engine if r['event'] == event][-1])
            reindex(engine)
        self.check('duplicate-wrapper', missing)
        def extra(rows, engine):
            index = next(i for i, r in enumerate(engine) if r['event'] == 'report.incoming')
            engine.insert(index, dict(engine[index])); reindex(engine)
        self.check('duplicate-wrapper', extra)
        def escaped(rows, engine):
            marker = [r for r in engine if r['event'] == 'contract.duplicateReportBoundary'][2]
            engine.remove(marker)
            engine.insert(engine.index([r for r in engine if r['event'] == 'report.incoming'][1]) + 1, marker)
            reindex(engine, reset_clock=True)
        self.check('duplicate-wrapper', escaped)

    def test_duplicate_real_end_and_cleanup(self):
        for event, field, value in (('contract.duplicateEndHandler', 'origin', 'system'),
                                   ('contract.duplicateEndHandler', 'actionID', 'wrong-action'),
                                   ('contract.duplicateEndCommandResult', 'accepted', False),
                                   ('contract.duplicateObservation', 'empty', False)):
            with self.subTest(event=event, field=field):
                self.check('duplicate-wrapper', lambda rows, engine:
                    next(r for r in rows if r['event'] == event).update({field: value}))
        for event, field, value in (('transaction.delivered', 'origin', 'app'),
                                   ('transaction.delivered', 'kinds', ['CXAnswerCallAction']),
                                   ('transaction.delivered', 'actionIDs', ['wrong-action']),
                                   ('action.settled', 'fulfilled', False),
                                   ('provider.apiKind', 'apiKind', 'callkit')):
            with self.subTest(event=event, field=field):
                self.check('duplicate-wrapper', lambda rows, engine:
                    next(r for r in engine if r['event'] == event).update({field: value}))
        self.check('duplicate-wrapper', lambda rows, engine:
            [r for r in engine if r['event'] == 'call.changed'][-1].update(ended=False))
        self.check('duplicate-wrapper', lambda rows, engine:
            [r for r in engine if r['event'] == 'contract.duplicateEndBoundary'][-1].update(empty=False))
        self.check('duplicate-wrapper', lambda rows, engine:
            [r for r in rows if r['event'] == 'contract.duplicateSnapshot'][-1].update(
                conversations=[dict(id=CALL, status='ended', hasConnectedAt=False)]))
        def shortcut(rows, engine):
            engine.insert(-1, dict(engine[-1], event='report.ended', callUUID=CALL)); reindex(engine)
        self.check('duplicate-wrapper', shortcut)

    def check(self, case=None, edit=None, file_edit=None, succeeds=False, parent=None):
        with tempfile.TemporaryDirectory(prefix='sdk-m1-contract-controls-') as temporary:
            root = Path(temporary); build = root / 'build'; capture = root / 'capture'
            build.mkdir(); capture.mkdir(); fixture(build, capture)
            if case:
                name = 'sdk-m1-contract-' + case
                rows = gate.load(capture, name)
                engine = [json.loads(line) for line in (capture / (name + '.runtime.jsonl')).read_text().splitlines()]
                edit(rows, engine); save(capture, case, rows, engine)
            if file_edit: file_edit(build, capture)
            parent_path = None
            if parent is not None:
                parent_path = root / 'parent-manifest.json'
                write(parent_path, parent)
            with patch.object(gate, 'source_identity', return_value=IDENTITY):
                if succeeds: self.assertTrue(gate.validate(build, capture, parent_manifest=parent_path)['success'])
                else:
                    with self.assertRaises((gate.EvidenceError, OSError, ValueError, KeyError, TypeError)):
                        gate.validate(build, capture, parent_manifest=parent_path)

    def test_positive(self): self.check(succeeds=True)

    def test_parent_sdk_run_binding(self):
        parent = dict(runID='synthetic-run', simulatorID='synthetic-simulator', sourceCommit=IDENTITY['sourceCommit'])
        self.check(parent=parent, succeeds=True)
        for key in parent:
            with self.subTest(key=key):
                self.check(parent={**parent, key: 'different-' + key})

    def test_guard_warnings(self):
        for field, value in [('main', False), ('message', 'monitor disabled'), ('count', 0), ('phase', 'owned')]:
            with self.subTest(field=field):
                self.check('guard', lambda rows, engine: next(r for r in rows if r['event'] == 'contract.warning').update({field: value}))
        self.check('guard', lambda rows, engine: next(r for r in rows if r['event'] == 'contract.owner').update(warnings=1))
        self.check('guard', lambda rows, engine: next(r for r in rows if r['event'] == 'contract.creationEnd').update(identity='wrong-provider'))

    def test_pending_deadline(self):
        self.check('unhandled-accept', lambda rows, engine: next(r for r in rows if r['event'] == 'contract.pending').update(deadlineMS=WALL + 5000, remainingMS=4990))
        self.check('unhandled-accept', lambda rows, engine: next(r for r in rows if r['event'] == 'contract.pending').update(handlerAttached=True))
        def early(rows, engine):
            row = next(r for r in rows if r['event'] == 'contract.snapshot' and r['conversations'] and r['conversations'][0]['status'] == 'connecting')
            row['wallTimeMS'] = WALL + 1000
        self.check('unhandled-accept', early)
        self.check('unhandled-accept', lambda rows, engine: next(r for r in rows if r['event'] == 'contract.snapshot' and r['conversations'] and 'pending' in r['conversations'][0])['conversations'][0]['pending'].update(id='different-action'))

    def test_native_settlement(self):
        self.check('unhandled-accept', lambda rows, engine: next(r for r in engine if r['event'] == 'action.settled').update(fulfilled=False))
        self.check('unhandled-accept', lambda rows, engine: next(r for r in engine if r['event'] == 'call.changed').update(connected=False))
        self.check('unhandled-accept', lambda rows, engine: next(r for r in rows if r['event'] == 'contract.snapshot' and r['conversations'] and r['conversations'][0]['status'] == 'connected')['conversations'][0].update(hasConnectedAt=False))

    def test_backend_connection_projection(self):
        for field, value in (('status', 'connected'), ('hasConnectedAt', True), ('pending', dict(kind='join'))):
            with self.subTest(field=field):
                self.check('unhandled-accept', lambda rows, engine, field=field, value=value:
                    next(r for r in rows if r['event'] == 'contract.snapshot' and r['conversations'] and
                         r['conversations'][0]['status'] == 'connecting')['conversations'][0].update({field: value}))

    def test_backend_report_marker_identity_and_order(self):
        for event in ('contract.joinSettled', 'contract.backendConnectedRequested', 'contract.backendConnected'):
            with self.subTest(event=event):
                self.check('unhandled-accept', lambda rows, engine, event=event:
                    next(r for r in rows if r['event'] == event).update(conversationID='other-call'))
                self.check('unhandled-accept', lambda rows, engine, event=event:
                    next(r for r in rows if r['event'] == event).pop('conversationID'))
                self.check('unhandled-accept', lambda rows, engine, event=event:
                    rows.remove(next(r for r in rows if r['event'] == event)))
        self.check('unhandled-accept', lambda rows, engine:
            next(r for r in rows if r['event'] == 'contract.joinSettled').update(joined=False))
        self.check('unhandled-accept', lambda rows, engine:
            next(r for r in rows if r['event'] == 'contract.backendConnected').update(accepted=False))
        def move(rows, engine, event, before):
            marker = next(r for r in rows if r['event'] == event)
            rows.remove(marker)
            target = next(r for r in rows if r['event'] == before)
            marker.update(t_ms=target['t_ms'], wallTimeMS=target['wallTimeMS'])
            rows.insert(rows.index(target), marker)
            reindex(rows)
        for event, before in (('contract.backendConnectedRequested', 'contract.joinSettled'),
                              ('contract.backendConnectedRequested', 'contract.backendConnected'),
                              ('contract.backendConnected', 'contract.backendConnectedRequested')):
            with self.subTest(event=event, before=before):
                self.check('unhandled-accept', lambda rows, engine, event=event, before=before:
                    move(rows, engine, event, before))

    def test_native_early_late_timing(self):
        def timing(rows, engine, settlement, connection):
            next(r for r in engine if r['event'] == 'action.settled')['t_ms'] = settlement
            next(r for r in engine if r['event'] == 'call.changed')['t_ms'] = connection
            for row in engine:
                if row['event'] == 'report.ended' or row.get('ended') is True:
                    row['t_ms'] = max(row['t_ms'], connection + 10)
        for settlement, connection in ((1000, 1001), (40_000, 40_001), (30_000, 40_000)):
            with self.subTest(settlement=settlement, connection=connection):
                self.check('unhandled-accept', lambda rows, engine: timing(rows, engine, settlement, connection))

    def test_fail_can_leave_conversation_for_explicit_cleanup(self):
        # The base fixture deliberately uses distinct LCK and CX UUIDs and
        # leaves a ringing conversation until the explicit failed report.
        self.check(succeeds=True)
        def delayed_native_forwarding(rows, engine):
            # Cleanup already waits through the actual recorded SDK expiry;
            # forwarding to CX has no measured synchronous budget.
            next(r for r in engine if r['event'] == 'action.settled')['t_ms'] += 100
        self.check('unhandled-fail', delayed_native_forwarding, succeeds=True)

    def test_fail_observation_and_cleanup_wait_for_platform_boundary(self):
        self.check('unhandled-fail', lambda rows, engine:
            next(r for r in rows if r['event'] == 'contract.failureObservation').update(wallTimeMS=WALL + 29_999, t_ms=29_999))
        def early_cleanup(rows, engine):
            for row in rows:
                if row['wallTimeMS'] >= WALL + 30_001:
                    row['wallTimeMS'] -= 20; row['t_ms'] -= 20
            for row in engine:
                if row['event'] in ('report.ended', 'call.changed'): row['t_ms'] -= 20
        self.check('unhandled-fail', early_cleanup)
        def sdk_expiry_before_runtime_expiry(rows, engine):
            next(r for r in engine if r['event'] == 'action.deadline')['deadlineMS'] += 100
            next(r for r in engine if r['event'] == 'action.settled')['t_ms'] = 30_000
        self.check('unhandled-fail', sdk_expiry_before_runtime_expiry)
        def runtime_expiry_before_sdk_expiry(rows, engine):
            for row in rows:
                if row['event'] == 'contract.actionDiagnostic': row['platformDeadlineMS'] += 1
            next(r for r in engine if r['event'] == 'action.settled')['t_ms'] = 30_000
        self.check('unhandled-fail', runtime_expiry_before_sdk_expiry)

    def test_fail_can_observe_absence_and_skip_cleanup(self):
        def absent(rows, engine):
            observation = next(r for r in rows if r['event'] == 'contract.failureObservation')
            clear = [r for r in rows[:rows.index(observation)] if r['event'] == 'contract.snapshot'][-1]
            clear['conversations'] = []
            observation.update(conversationPresent=False, status='absent')
            for row in rows[:]:
                if row['event'] == 'contract.fixtureCleanupRequested':
                    row['event'] = 'contract.fixtureCleanupSkipped'; row.pop('reason')
                elif row['event'] == 'contract.fixtureCleanupReported' or (row['event'] == 'contract.snapshot' and rows.index(row) > rows.index(observation)):
                    rows.remove(row)
            engine[:] = [r for r in engine if r['event'] != 'report.ended']
            next(r for r in engine if r['event'] == 'call.changed')['t_ms'] = 28_502
            renumber(rows, engine)
        self.check('unhandled-fail', absent, succeeds=True)

    def test_fail_allows_only_later_system_end(self):
        self.check('unhandled-fail', later_system_end, succeeds=True)
        def end_pending(rows, engine):
            later_system_end(rows, engine, settle=True)
            snapshot = next(r for r in rows if r['event'] == 'contract.snapshot' and r['wallTimeMS'] == WALL + 28_503)
            snapshot['conversations'][0]['pending'] = dict(id='lck-system-end', kind='end', origin='system', deadlineMS=WALL + 57_002)
        self.check('unhandled-fail', end_pending, succeeds=True)
        for field, value in (('origin', 'application'), ('kinds', ['CXStartCallAction']), ('provider', 'unrelated'), ('requestID', 'app-request')):
            def unrelated(rows, engine, field=field, value=value):
                later_system_end(rows, engine)
                next(r for r in engine if r['event'] == 'transaction.delivered' and r['origin'] == 'system')[field] = value
            with self.subTest(field=field): self.check('unhandled-fail', unrelated)
        def end_cannot_substitute(rows, engine):
            later_system_end(rows, engine, settle=True)
            next(r for r in engine if r['event'] == 'action.settled')['fulfilled'] = True
        self.check('unhandled-fail', end_cannot_substitute)

    def test_fail_requires_all_bound_diagnostics(self):
        for phase in ('policy-fire', 'platform-settle-enter', 'platform-settle-return', 'pending-state-published'):
            def missing(rows, engine, phase=phase):
                rows.remove(next(r for r in rows if r.get('phase') == phase)); renumber(rows)
            with self.subTest(missing=phase): self.check('unhandled-fail', missing)
        for field, value in (('actionID', 'wrong-lck-action'), ('phase', 'policy-fire'),
                             ('effectiveDeadlineMS', WALL + 28_499), ('platformDeadlineMS', WALL + 31_000),
                             ('sdkWallTimeMS', WALL + 1_000), ('entryPending', True), ('platformPending', None)):
            with self.subTest(field=field):
                self.check('unhandled-fail', lambda rows, engine, field=field, value=value:
                    next(r for r in rows if r.get('phase') == 'platform-settle-return').update({field: value}))
        for phase, field, value in (('policy-fire', 'entryPending', False), ('policy-fire', 'platformPending', False),
                                    ('platform-settle-enter', 'platformPending', False)):
            with self.subTest(phase=phase, field=field):
                self.check('unhandled-fail', lambda rows, engine, phase=phase, field=field, value=value:
                    next(r for r in rows if r.get('phase') == phase).update({field: value}))

    def test_fail_diagnostics_and_settlement_cannot_be_early_or_late(self):
        def shift_policy(rows, engine, delta):
            begin = next(i for i, r in enumerate(rows) if r['event'] == 'contract.actionDiagnostic')
            for row in rows[begin:]:
                row['wallTimeMS'] += delta; row['t_ms'] += delta
                if 'sdkWallTimeMS' in row: row['sdkWallTimeMS'] += delta
            for row in engine:
                if row['t_ms'] >= 28_500: row['t_ms'] += delta
        for delta in (-5, 1500):
            with self.subTest(delta=delta):
                self.check('unhandled-fail', lambda rows, engine, delta=delta: shift_policy(rows, engine, delta))
        def late_native(rows, engine):
            for row in engine:
                if row['t_ms'] >= 28_500: row['t_ms'] += 1499
        self.check('unhandled-fail', late_native)
        self.check('unhandled-fail', lambda rows, engine:
            next(r for r in rows if r.get('phase') == 'policy-fire').update(sdkWallTimeMS=WALL + 28_499))
        self.check('unhandled-fail', lambda rows, engine:
            next(r for r in engine if r['event'] == 'action.deadline').update(deadlineMS=WALL + 28_500))

    def test_fail_diagnostics_need_real_observer_clearing(self):
        def retained(rows, engine):
            pending = next(r for r in rows if r['event'] == 'contract.snapshot' and r['conversations'] and 'pending' in r['conversations'][0])['conversations'][0]['pending']
            for row in rows:
                if row['event'] == 'contract.snapshot' and row['wallTimeMS'] == WALL + 28_503:
                    row['conversations'][0]['pending'] = pending
        self.check('unhandled-fail', retained)
        def resurrected(rows, engine):
            retained(rows, engine)
            snapshot = next(r for r in rows if r['event'] == 'contract.snapshot' and r['wallTimeMS'] == WALL + 28_503)
            rows.remove(snapshot)
            index = next(i for i, r in enumerate(rows) if r['event'] == 'contract.failureObservation')
            snapshot.update(wallTimeMS=rows[index]['wallTimeMS'], t_ms=rows[index]['t_ms'])
            rows.insert(index, snapshot); renumber(rows)
        self.check('unhandled-fail', resurrected)
        self.check('unhandled-fail', lambda rows, engine:
            next(r for r in rows if r['event'] == 'contract.failureObservation').update(status='ended'))

    def test_fail_refuses_wrong_policy_identity_and_connection(self):
        for event, field, value in (('contract.configuration', 'policy', 'accept'),
                                    ('contract.configuration', 'handlerAttached', True),
                                    ('contract.pending', 'actionID', 'different-lck-action'),
                                    ('contract.failureObservation', 'conversationPresent', False),
                                    ('contract.failureObservation', 'pendingCleared', False)):
            with self.subTest(event=event, field=field):
                self.check('unhandled-fail', lambda rows, engine, event=event, field=field, value=value:
                    next(r for r in rows if r['event'] == event).update({field: value}))
        for field, value in (('actionID', 'unknown-cx-action'), ('fulfilled', True)):
            self.check('unhandled-fail', lambda rows, engine, field=field, value=value:
                next(r for r in engine if r['event'] == 'action.settled').update({field: value}))
        self.check('unhandled-fail', lambda rows, engine:
            next(r for r in engine if r['event'] == 'contract.pendingClock').update(actionID='wrong-lck-action'))
        self.check('unhandled-fail', lambda rows, engine:
            next(r for r in engine if r['event'] == 'call.changed').update(connected=True))
        for field, value in (('status', 'connected'), ('hasConnectedAt', True)):
            self.check('unhandled-fail', lambda rows, engine, field=field, value=value:
                next(r for r in rows if r['event'] == 'contract.snapshot' and r['wallTimeMS'] == WALL + 28_503)['conversations'][0].update({field: value}))

    def test_fail_refuses_fabricated_cleanup(self):
        for event, field, value in (('contract.fixtureCleanupRequested', 'reason', 'remoteEnded'),
                                    ('contract.fixtureCleanupReported', 'conversationID', 'wrong-call'),
                                    ('contract.fixtureCleanupRequested', 'event', 'contract.fixtureCleanupSkipped')):
            with self.subTest(event=event, field=field):
                self.check('unhandled-fail', lambda rows, engine, event=event, field=field, value=value:
                    next(r for r in rows if r['event'] == event).update({field: value}))
        for field, value in (('callUUID', 'wrong-call'), ('reason', 2)):
            self.check('unhandled-fail', lambda rows, engine, field=field, value=value:
                next(r for r in engine if r['event'] == 'report.ended').update({field: value}))
        def no_native_report(rows, engine):
            engine.remove(next(r for r in engine if r['event'] == 'report.ended')); renumber(engine)
        self.check('unhandled-fail', no_native_report)
        def cleanup_before_observation(rows, engine):
            observation = next(r for r in rows if r['event'] == 'contract.failureObservation')
            requested = next(r for r in rows if r['event'] == 'contract.fixtureCleanupRequested')
            rows[rows.index(observation)], rows[rows.index(requested)] = requested, observation
            requested['wallTimeMS'], observation['wallTimeMS'] = observation['wallTimeMS'], requested['wallTimeMS']
            renumber(rows)
        self.check('unhandled-fail', cleanup_before_observation)

    def test_build_and_receipt_binding(self):
        self.check(file_edit=lambda build, capture: (build / gate.BUILD_FILES[0]).write_text('tampered'))
        self.check(file_edit=lambda build, capture: (capture / 'sdk-m1-contract-guard.runtime.jsonl').write_text(''))
        for key, value in (('configuration', 'Release'), ('product', 'different'), ('schemaVersion', 0)):
            def metadata(build, capture, key=key, value=value):
                path = build / 'build-info.json'
                info = json.loads(path.read_text()); info[key] = value; write(path, info)
            self.check(file_edit=metadata)
        def rehashed_source_after(build, capture):
            write(build / 'source-after.json', {**IDENTITY, 'sourceInputsSHA256': 'c' * 64})
            path = build / 'build-info.json'
            info = json.loads(path.read_text())
            info['files']['source-after.json'] = gate.digest(build / 'source-after.json')
            write(path, info)
        self.check(file_edit=rehashed_source_after)
        def bad_pid(build, capture):
            path = capture / 'sdk-m1-contract-guard.receipt.json'
            receipt = json.loads(path.read_text()); receipt['launcherAppPID'] = 999; write(path, receipt)
        self.check(file_edit=bad_pid)


if __name__ == '__main__': unittest.main()
