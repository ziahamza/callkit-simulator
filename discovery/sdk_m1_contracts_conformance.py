#!/usr/bin/env python3
"""Debug native contracts; synthetic controls do not qualify native behavior."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

from device_oracles import check_deadline, deadline_bands
from sdk_m2_audio_lifecycle import validate_authority, validate_lifecycle
from sdk_m0_conformance import bound_rows, digest
from summarize import EvidenceError, load, one, require

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = 'dev.iosvoipsdk.m1contracts'
CASES = ('guard', 'unhandled-accept', 'unhandled-fail', 'duplicate-wrapper', 'media-reentrancy', 'audio-lifecycle', 'audio-routes')
OWN_FILES = ('discovery/probe/sdk_m1_contracts.swift', 'discovery/build_sdk_m1_contracts.sh',
             'discovery/run_audio_lifecycle_legacy.sh',
             'discovery/sdk_m1_contracts_conformance.py', 'discovery/check_sdk_m1_contracts.py',
             'discovery/probe/Info.plist', 'discovery/run_scenario.py', 'discovery/sdk_m0_conformance.py',
             'discovery/sdk_m2_audio_lifecycle.py', 'discovery/check_sdk_m2_audio_lifecycle.py',
             'discovery/sdk_bootstrap_conformance.py', 'discovery/summarize.py', 'discovery/device_oracles.py', 'VERSION')
REFERENCES = tuple('docs/research/callkit-simulator-probe/livecommunicationkit/2026-10-21-iphone13-ios27-lck-' + case + '.jsonl'
                   for case in ('basic', 'cx-after', 'lck-after', 'two-managers'))
WARNING = 'Another CXProvider was created. D13 routes actions to the newest provider; keep one ConversationManager in this process.'
BUILD_FILES = ('SDKM1Contracts.app/SDKM1Contracts', 'SDKM1Contracts.app/Info.plist',
               'SDKM1Contracts.app/IosVoipSDKRuntime.bundle/ios-27.0.json',
               'SDKM1Contracts.app/IosVoipSDKRuntime.bundle/Info.plist', 'build.log',
               'source-before.json', 'source-after.json')


def source_identity(root=ROOT):
    names = set(subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard',
                                        '-z', '--', 'Sources'], cwd=root).decode().split('\0'))
    names.discard(''); names.update((*OWN_FILES, *REFERENCES))
    value = hashlib.sha256()
    for name in sorted(names):
        value.update(name.encode() + b'\0' + (root / name).read_bytes() + b'\0')
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all', '--', *sorted(names)], cwd=root)
    return dict(sourceCommit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                sourceInputsSHA256=value.hexdigest(), sourceDirty=bool(dirty))


def record_build(directory, target):
    after = source_identity()
    (directory / 'source-after.json').write_text(json.dumps(after, indent=2) + '\n')
    require(json.loads((directory / 'source-before.json').read_text()) == after, 'sources changed during build')
    info = dict(schemaVersion=1, product='sdk-m1-contracts', configuration='Debug', target=target, **after,
                files={name: digest(directory / name) for name in BUILD_FILES},
                xcodeVersion=subprocess.check_output(['xcodebuild', '-version'], text=True).strip(),
                sdkVersion=subprocess.check_output(['xcrun', '--sdk', 'iphonesimulator', '--show-sdk-version'], text=True).strip())
    (directory / 'build-info.json').write_text(json.dumps(info, indent=2) + '\n')


def reentrancy_contract(rows, engine):
    owner = one(rows, 'contract.owner')
    result = one(rows, 'contract.mediaReentrancy')
    require(isinstance(owner.get('identity'), str) and owner['identity'] and
            owner['identity'] == owner.get('sharedIdentity') and owner.get('warnings') == 0,
            'reentrancy: process owner differs')
    require(type(result.get('authorizationCalls')) is int and result['authorizationCalls'] == 1 and
            result.get('sameOwner') is True and type(result.get('warnings')) is int and result['warnings'] == 0,
            'reentrancy: singleton/adapter callback contract failed')
    require(len([r for r in engine if r['event'] == 'provider.created']) == 1 and
            not any(r['event'] in ('transaction.delivered', 'call.changed') for r in engine),
            'reentrancy: unexpected provider or call activity')
    return dict(success=True, owners=1, authorizationCalls=1)


def routes_contract(rows, engine):
    owner = one(rows, 'contract.owner')
    require(isinstance(owner.get('identity'), str) and owner['identity'] and
            owner['identity'] == owner.get('sharedIdentity') and owner.get('warnings') == 0,
            'routes: process owner differs')
    require(len([r for r in engine if r['event'] == 'provider.created']) == 1,
            'routes: expected one provider')
    snapshots = []
    for phase in ('after', 'unchanged'):
        row = one(rows, 'routes.observed', phase=phase)
        inputs, outputs, snapshot = row.get('inputs'), row.get('outputs'), row.get('snapshot')
        require(isinstance(inputs, list) and isinstance(outputs, list) and isinstance(snapshot, dict),
                'routes: missing OS observations or snapshot')
        require(all(isinstance(port, dict) and set(port) == {'uid', 'name', 'type'} and
                    all(isinstance(port[key], str) and port[key] for key in port) for port in inputs + outputs),
                'routes: invalid OS port')
        expected = []
        for port in inputs:
            value = dict(id='input:' + port['uid'], name=port['name'], type=port['type'])
            if not any(route['id'] == value['id'] for route in expected): expected.append(value)
        expected.append(dict(id='speaker', name='Speaker', type='Speaker'))
        current = None
        if outputs:
            current = dict(id='output:' + outputs[0]['uid'], name=outputs[0]['name'], type=outputs[0]['type'])
            expected.append(current)
        require(type(snapshot.get('revision')) is int and snapshot['revision'] > 0 and
                snapshot.get('routes') == expected and 'current' in snapshot and snapshot['current'] == current,
                'routes: public snapshot differs from actual OS input/current output identities')
        snapshots.append(snapshot)
    command, noop = one(rows, 'routes.command'), one(rows, 'routes.currentNoOp')
    require(command.get('id') == 'speaker' and type(command.get('succeeded')) is bool,
            'routes: missing speaker command outcome')
    if not command['succeeded']:
        require(isinstance(command.get('errorDomain'), str) and command['errorDomain'] and
                type(command.get('errorCode')) is int, 'routes: missing native command error')
    require(noop.get('available') is (snapshots[0]['current'] is not None) and noop.get('unchanged') is True and
            snapshots[1] == snapshots[0], 'routes: already-current selection changed the observed snapshot')
    authority, ended = one(rows, 'routes.authority'), one(rows, 'routes.ended')
    require(isinstance(authority.get('conversationID'), str) and
            str(uuid.UUID(authority['conversationID'])).upper() == authority['conversationID'] and
            authority.get('authorized') is True and ended.get('conversationID') == authority['conversationID'] and
            ended.get('empty') is True and ended.get('authorized') is False,
            'routes: real LCK call authority/cleanup differs')
    ordered = [owner, authority, command,
               one(rows, 'routes.observed', phase='after'), noop,
               one(rows, 'routes.observed', phase='unchanged'), ended]
    require([rows.index(row) for row in ordered] == sorted(rows.index(row) for row in ordered),
            'routes: observation/command/cleanup order differs')
    call = authority['conversationID']
    changes = [r for r in engine if r['event'] == 'call.changed']
    transactions = [r for r in engine if r['event'] == 'transaction.delivered']
    settled = [r for r in engine if r['event'] == 'action.settled']
    require(changes and all(r.get('uuid') == call for r in changes) and changes[-1].get('ended') is True and
            [r.get('kinds') for r in transactions] == [['CXAnswerCallAction'], ['CXEndCallAction']] and
            all(isinstance(r.get('actionIDs'), list) and len(r['actionIDs']) == 1 for r in transactions) and
            [r.get('actionID') for r in settled] == [r['actionIDs'][0] for r in transactions] and
            all(r.get('fulfilled') is True for r in settled) and
            [r['event'] for r in engine if r['event'] in ('audio.activated', 'audio.deactivated')] ==
            ['audio.activated', 'audio.deactivated'] and
            [r.get('active') for r in rows if r['event'] == 'routes.lckDelegate'] == [True, False],
            'routes: same-call native join/end/audio ledger differs')
    return dict(success=True, speakerCommandSucceeded=command['succeeded'], observedSnapshots=2,
                classification='native OS route observations; no hardware or fold claim')


def guard_contract(rows, engine):
    owner = one(rows, 'contract.owner')
    require(owner.get('warnings') == 0 and isinstance(owner.get('identity'), str) and
            owner['identity'] and owner['identity'] == owner.get('sharedIdentity'), 'SDK singleton/owned warning mismatch')
    warnings = [r for r in rows if r['event'] == 'contract.warning']
    require([(r.get('phase'), r.get('count'), r.get('message')) for r in warnings] ==
            [(phase, count, WARNING) for phase, count in [('extra-cx', 1), ('extra-lck', 2)]],
            'foreign provider warning inventory/identity differs')
    created = [r for r in engine if r['event'] == 'provider.created']
    require(len(created) == 3 and len({r.get('provider') for r in created}) == 3, 'expected exactly three fixture providers')
    identities = [owner['identity']]
    for phase, count, warning in zip(('extra-cx', 'extra-lck'), (1, 2), warnings):
        begin = one(rows, 'contract.creationBegin', phase=phase)
        end = one(rows, 'contract.creationEnd', phase=phase)
        require(rows.index(owner) < rows.index(begin) < rows.index(warning) < rows.index(end) and
                end.get('warnings') == count and isinstance(end.get('identity'), str) and end['identity'],
                'foreign provider warning order/count differs')
        identities.append(end['identity'])
    require(len(set(identities)) == 3 and identities[1] == created[1]['provider'], 'foreign provider identity differs')
    return dict(success=True, warnings=2, providers=3)


def duplicate_contract(rows, engine):
    """Public duplicate result through genuine LCK, not NSError-depth inspection.

    `.pushKit` is a public API source argument here, not a real push delivery.
    The runtime supplies CX service behavior; the LCK framework is unchanged.
    """
    owner = one(rows, 'contract.owner')
    require(type(owner.get('warnings')) is int and owner['warnings'] == 0 and
            isinstance(owner.get('identity'), str) and owner['identity'] and
            owner['identity'] == owner.get('sharedIdentity') and owner.get('entryPoint') == 'VoIPApplication.start',
            'duplicate public application owner differs')
    require(not any(r['event'] in ('contract.warning', 'contract.duplicateUnexpectedAction') for r in rows),
            'duplicate contract received an unexpected owner/action')
    requests = [r for r in rows if r['event'] == 'contract.duplicateReportRequested']
    results = [r for r in rows if r['event'] == 'contract.duplicateReportResult']
    require(len(requests) == len(results) == 2, 'duplicate requires two public report attempts/results')
    call = results[0].get('conversationID')
    require(isinstance(call, str) and str(uuid.UUID(call)).upper() == call, 'invalid duplicate conversation UUID')
    payload_hash = requests[0].get('payloadSHA256')
    require(isinstance(payload_hash, str) and re.fullmatch('[0-9a-f]{64}', payload_hash), 'invalid duplicate payload digest')
    previous = rows.index(owner)
    markers = [r for r in engine if r['event'] == 'contract.duplicateReportBoundary']
    incoming = [r for r in engine if r['event'] == 'report.incoming']
    completed = [r for r in engine if r['event'] == 'report.completed']
    require(len(markers) == 4 and len(incoming) == len(completed) == 2,
            'duplicate must enter and complete two native reports')
    for index, tag in enumerate(('foreground', 'pushKit')):
        ordinal = index + 1
        request, result = requests[index], results[index]
        for row in (request, result):
            require(type(row.get('ordinal')) is int and row['ordinal'] == ordinal and
                    row.get('source') == tag and row.get('payloadSHA256') == payload_hash,
                    'duplicate public payload/source/order differs')
        require(result.get('accepted') is True and result.get('conversationID') == call and
                result.get('conversations') == [dict(id=call, status='ringing', hasConnectedAt=False, hasPending=False)] and
                result['conversations'][0].get('hasConnectedAt') is False and
                result['conversations'][0].get('hasPending') is False,
                'duplicate result must preserve one unconnected ringing conversation')
        require(previous < rows.index(request) < rows.index(result), 'duplicate public awaits overlap or reorder')
        previous = rows.index(result)
        before, after = markers[index * 2:index * 2 + 2]
        for marker, stage in ((before, 'requested'), (after, 'returned')):
            require(type(marker.get('ordinal')) is int and marker['ordinal'] == ordinal and
                    marker.get('stage') == stage and marker.get('source') == tag and
                    marker.get('payloadSHA256') == payload_hash, 'unbound duplicate native marker')
        require(after.get('callUUID') == call and incoming[index].get('callUUID') == call and
                completed[index].get('callUUID') == call and type(completed[index].get('error')) is int and
                completed[index]['error'] == (0, 2)[index], 'native duplicate completion identity/error differs')
        require(engine.index(before) < engine.index(incoming[index]) < engine.index(completed[index]) < engine.index(after),
                'native report escaped its awaited public call')
    require(engine.index(markers[1]) < engine.index(markers[2]), 'native duplicate calls overlap')
    end_request = one(rows, 'contract.duplicateEndRequested')
    handler = one(rows, 'contract.duplicateEndHandler')
    end_result = one(rows, 'contract.duplicateEndCommandResult')
    observation = one(rows, 'contract.duplicateObservation')
    require(all(r.get('conversationID') == call for r in (end_request, handler, end_result, observation)) and
            handler.get('origin') == 'app' and isinstance(handler.get('actionID'), str) and
            str(uuid.UUID(handler['actionID'])).upper() == handler['actionID'] and
            end_result.get('accepted') is True and observation.get('empty') is True and
            previous < rows.index(end_request) < rows.index(handler) < rows.index(observation) and
            rows.index(end_request) < rows.index(end_result) < rows.index(observation),
            'duplicate cleanup lacks public end request/handler/command result')
    snapshots = [r for r in rows if r['event'] == 'contract.duplicateSnapshot']
    require(snapshots and all(type(r.get('revision')) is int for r in snapshots) and
            all(a['revision'] < b['revision'] for a, b in zip(snapshots, snapshots[1:])), 'invalid duplicate snapshot revisions')
    pending = []
    for snapshot in snapshots:
        calls = snapshot.get('conversations')
        require(isinstance(calls, list) and len(calls) <= 1, 'duplicate projected another conversation')
        for value in calls:
            require(value.get('id') == call and value.get('status') in ('ringing', 'ending', 'ended') and
                    value.get('hasConnectedAt') is False, 'duplicate projection identity/state differs')
            if 'pending' in value:
                require(value['pending'] == dict(id=handler['actionID'], kind='end', origin='app') and
                        rows.index(end_request) < rows.index(snapshot) < rows.index(handler),
                        'duplicate pending action is not the observed public end')
                pending.append(snapshot)
    require(pending and snapshots[-1]['conversations'] == [] and
            rows.index(end_request) < rows.index(snapshots[-1]) < rows.index(observation),
            'duplicate cleanup lacks pending/empty SDK projections')
    created = [r for r in engine if r['event'] == 'provider.created']
    classifications = [r for r in engine if r['event'] == 'provider.apiKind' and r.get('delegatePresent') is True]
    require(len(created) == 1 and isinstance(created[0].get('provider'), str) and created[0]['provider'] and classifications and
            all(r.get('provider') == created[0].get('provider') and r.get('apiKind') == 'livecommunicationkit'
                for r in classifications), 'duplicate did not use one genuine LCK provider')
    boundaries = [r for r in engine if r['event'] == 'contract.duplicateEndBoundary']
    delivered = [r for r in engine if r['event'] == 'transaction.delivered']
    settled = [r for r in engine if r['event'] == 'action.settled']
    require(len(boundaries) == 3 and [r.get('stage') for r in boundaries] ==
            ['requested', 'command-returned', 'observed-complete'] and
            all(r.get('callUUID') == call for r in boundaries) and boundaries[-1].get('empty') is True,
            'unbound duplicate native end request/completion observation')
    require(len(delivered) == len(settled) == 1 and delivered[0].get('kinds') == ['CXEndCallAction'] and
            delivered[0].get('origin') == 'application' and delivered[0].get('provider') == created[0].get('provider') and
            isinstance(settled[0].get('actionID'), str) and settled[0]['actionID'] and
            delivered[0].get('actionIDs') == [settled[0]['actionID']] and settled[0].get('fulfilled') is True,
            'duplicate native cleanup was not one fulfilled app End action')
    states = [r for r in engine if r['event'] == 'call.changed']
    require(len(states) == 2 and all(r.get('uuid') == call and r.get('outgoing') is False and r.get('connected') is False for r in states) and
            states[0].get('ended') is False and states[-1].get('ended') is True and
            engine.index(incoming[0]) < engine.index(states[0]) < engine.index(completed[0]) and
            engine.index(markers[-1]) < engine.index(boundaries[0]) < engine.index(delivered[0]) <
            engine.index(settled[0]) < engine.index(states[-1]) < engine.index(boundaries[-1]),
            'duplicate native call did not end before its explicit completion observation')
    require(not any(r['event'] in ('action.timeout', 'provider.reset', 'report.ended') for r in engine),
            'duplicate cleanup timed out/reset or used a report-ended shortcut')
    return dict(success=True, nativeReportCompletionErrors=[0, 2], publicResultsSameUUID=True,
                liveConversations=1, cleanup='public app End action',
                classification='genuine LCK public-result compatibility on the simulator runtime',
                limits='Public source tag only; no PushKit delegate, NSError nesting-depth or physical-device claim.')


def accept_contract(rows, engine, source_root):
    configuration = one(rows, 'contract.configuration')
    require(configuration.get('policy') == 'accept' and configuration.get('handlerAttached') is False and
            rows.index(configuration) < rows.index(one(rows, 'contract.owner')), 'accept policy configured too late')
    require(not any(r['event'] == 'contract.warning' for r in rows) and one(rows, 'contract.owner').get('warnings') == 0,
            'unexpected provider ownership warning')
    incoming = one(rows, 'contract.incoming'); call = incoming.get('conversationID')
    pending = one(rows, 'contract.pending'); deadline = pending.get('deadlineMS')
    require(isinstance(call, str) and call and pending.get('conversationID') == call and
            pending.get('kind') == 'join' and pending.get('origin') == 'app' and
            pending.get('handlerAttached') is False and isinstance(pending.get('actionID'), str) and
            pending['actionID'] and type(deadline) is int, 'invalid native pending join')
    require(abs(deadline - pending['wallTimeMS'] - pending['remainingMS']) <= 2, 'pending deadline sample differs')
    reference = [row for name in REFERENCES for row in load(source_root / Path(name).parent, Path(name).stem)]
    # Device samples measure the platform deadline. The public deadline is the
    # application policy boundary, 1.5 seconds earlier, including for JS.
    check_deadline(dict(kind='JoinConversationAction', timeoutIn=pending['remainingMS'] / 1000 + 1.5), deadline_bands(reference))
    advertised = one(engine, 'action.deadline')
    platform_deadline = advertised.get('deadlineMS')
    require(advertised.get('apiKind') == 'livecommunicationkit' and type(platform_deadline) is int and
            advertised.get('deadlineSeconds') == advertised.get('expirySeconds') == 30 and
            1000 <= platform_deadline - deadline <= 3500,
            'effective deadline lacks the LCK safety margin or uses provider timing')
    snapshots = [r for r in rows if r['event'] == 'contract.snapshot']
    require(snapshots and all(type(r.get('revision')) is int for r in snapshots) and
            all(a['revision'] < b['revision'] for a, b in zip(snapshots, snapshots[1:])), 'invalid snapshot revision')
    joined = one(rows, 'contract.joinSettled')
    backend_requested = one(rows, 'contract.backendConnectedRequested')
    backend = one(rows, 'contract.backendConnected')
    observation = one(rows, 'contract.observation')
    require(joined.get('joined') is True and backend.get('accepted') is True and
            all(row.get('conversationID') == call for row in (joined, backend_requested, backend, observation)) and
            rows.index(pending) < rows.index(joined) < rows.index(backend_requested) < rows.index(backend) < rows.index(observation),
            'signaling acceptance must precede the explicit backend report')
    before_joined = [row for row in snapshots if rows.index(row) < rows.index(joined) and row['conversations']]
    require(before_joined and before_joined[-1]['conversations'][0].get('status') == 'connecting' and
            before_joined[-1]['conversations'][0].get('pending') is None and
            deadline - 1 <= before_joined[-1]['wallTimeMS'] <= joined['wallTimeMS'] <= deadline + 3000 and
            joined['wallTimeMS'] < platform_deadline,
            'no deadline-bound signaling settlement before backend connection')
    connected = []
    native_pending = []
    for row in snapshots:
        calls = row.get('conversations')
        require(isinstance(calls, list) and len(calls) <= 1, 'duplicate conversation projection')
        for value in calls:
            require(value.get('id') == call and type(value.get('hasConnectedAt')) is bool, 'unbound/invalid connected report')
            require(value.get('status') in ('ringing', 'connecting', 'connected', 'ending', 'ended'), 'unexpected accept state')
            if 'pending' in value:
                action = value['pending']
                require(action == dict(id=pending['actionID'], kind='join', origin='app', deadlineMS=deadline), 'pending identity/deadline changed')
                native_pending.append(row)
            if rows.index(row) < rows.index(backend_requested):
                require(value.get('status') != 'connected' and value['hasConnectedAt'] is False,
                        'join signaling fabricated transport connection')
            if value.get('status') == 'connected':
                require(rows.index(row) > rows.index(backend_requested) and value.get('pending') is None and
                        value['hasConnectedAt'] is True, 'connected projection lacks the explicit backend report')
                connected.append(row)
    require(native_pending and native_pending[0]['wallTimeMS'] < deadline and connected and
            rows.index(joined) < rows.index(connected[0]) < rows.index(observation),
            'connected state missing after signaling and backend report')
    require(observation.get('connected') is True and
            one(rows, 'contract.joinResult').get('accepted') is True, 'join never became connected')
    end_request = one(rows, 'contract.endReportRequested')
    ended = one(rows, 'contract.reportEnded')
    require(ended.get('conversationID') == call and rows.index(connected[0]) < rows.index(ended) and
            end_request.get('conversationID') == call and rows.index(connected[0]) < rows.index(end_request) <
            rows.index(snapshots[-1]) and snapshots[-1]['conversations'] == [], 'reported cleanup did not empty the SDK')
    delivered = [r for r in engine if r['event'] == 'transaction.delivered']
    settled = [r for r in engine if r['event'] == 'action.settled']
    require(len(delivered) == len(settled) == 1 and delivered[0].get('kinds') == ['CXAnswerCallAction'] and
            delivered[0].get('actionIDs') == [settled[0].get('actionID')] and settled[0].get('fulfilled') is True and
            advertised.get('actionID') == settled[0].get('actionID') and
            not any(r['event'] == 'action.timeout' for r in engine), 'native answer settlement differs')
    native_connected = [r for r in engine if r['event'] == 'call.changed' and r.get('uuid') == call and
                        r.get('connected') is True and r.get('ended') is False]
    require(native_connected, 'native call never connected')
    marker = one(engine, 'contract.pendingClock')
    require(marker.get('actionID') == pending['actionID'] and type(pending.get('clockBeforeMS')) is int and
            0 <= pending['wallTimeMS'] - pending['clockBeforeMS'] <= 25, 'unbound or imprecise native clock sample')
    # The marker is synchronously journaled between two native wall-clock reads.
    # A 25 ms binding budget covers that interval; it never changes action expiry.
    for event in (settled[0], native_connected[0]):
        relative = event['t_ms'] - marker['t_ms']
        require(pending['clockBeforeMS'] + relative >= deadline - 25 and
                pending['wallTimeMS'] + relative < platform_deadline, 'native settlement/connection must precede platform timeout')
    native_end = one(engine, 'report.ended')
    call_states = [r for r in engine if r['event'] == 'call.changed' and r.get('uuid') == call]
    require(native_end.get('callUUID') == call and call_states[-1].get('ended') is True and
            engine.index(delivered[0]) < engine.index(marker) < engine.index(settled[0]) <
            engine.index(native_connected[0]) < engine.index(native_end) < engine.index(call_states[-1]),
            'native delivery/settlement/connection/cleanup order differs')
    require(len([r for r in engine if r['event'] == 'provider.created']) == 1, 'accept created another provider')
    return dict(success=True, effectiveDeadlineMS=deadline, runtimeDeadlineMS=platform_deadline,
                signalingAfterEffectiveDeadlineMS=joined['wallTimeMS'] - deadline,
                signalingBeforeRuntimeDeadlineMS=platform_deadline - joined['wallTimeMS'], explicitBackendConnected=True)


def fail_contract(rows, engine, source_root):
    configuration = one(rows, 'contract.configuration'); owner = one(rows, 'contract.owner')
    require(configuration.get('policy') == 'fail' and configuration.get('handlerAttached') is False and
            rows.index(configuration) < rows.index(owner), 'fail policy configured too late')
    require(not any(r['event'] == 'contract.warning' for r in rows) and owner.get('warnings') == 0 and
            owner.get('identity') and owner['identity'] == owner.get('sharedIdentity'), 'unexpected fail owner/warning')
    incoming = one(rows, 'contract.incoming'); call = incoming.get('conversationID')
    pending = one(rows, 'contract.pending'); deadline = pending.get('deadlineMS'); action_id = pending.get('actionID')
    require(isinstance(call, str) and call and pending.get('conversationID') == call and
            pending.get('kind') == 'join' and pending.get('origin') == 'app' and
            pending.get('handlerAttached') is False and isinstance(action_id, str) and action_id and
            type(deadline) is int and type(pending.get('remainingMS')) is int and
            abs(deadline - pending['wallTimeMS'] - pending['remainingMS']) <= 2 and
            rows.index(owner) < rows.index(incoming) < rows.index(pending), 'invalid fail pending join')
    reference = [row for name in REFERENCES for row in load(source_root / Path(name).parent, Path(name).stem)]
    check_deadline(dict(kind='JoinConversationAction', timeoutIn=pending['remainingMS'] / 1000 + 1.5), deadline_bands(reference))
    marker = one(engine, 'contract.pendingClock')
    require(marker.get('actionID') == action_id and type(pending.get('clockBeforeMS')) is int and
            0 <= pending['wallTimeMS'] - pending['clockBeforeMS'] <= 25, 'unbound or imprecise fail clock sample')
    def native_wall(event):
        relative = event['t_ms'] - marker['t_ms']
        return pending['clockBeforeMS'] + relative, pending['wallTimeMS'] + relative

    delivered = [r for r in engine if r['event'] == 'transaction.delivered']
    answer = one(delivered, 'transaction.delivered', kinds=['CXAnswerCallAction'])
    require(answer.get('origin') == 'application' and isinstance(answer.get('provider'), str) and
            answer['provider'] and isinstance(answer.get('actionIDs'), list) and len(answer['actionIDs']) == 1 and
            isinstance(answer['actionIDs'][0], str) and answer['actionIDs'][0], 'invalid native fail answer')
    native_id = answer['actionIDs'][0]
    settled = one(engine, 'action.settled', actionID=native_id)
    advertised = one(engine, 'action.deadline', actionID=native_id); platform_deadline = advertised.get('deadlineMS')
    require(settled.get('fulfilled') is False and settled.get('origin') == 'application' and
            advertised.get('apiKind') == 'livecommunicationkit' and type(platform_deadline) is int and
            advertised.get('provider') == answer['provider'] and
            advertised.get('deadlineSeconds') == advertised.get('expirySeconds') == 30 and
            1000 <= platform_deadline - deadline <= 3500 and
            engine.index(answer) < engine.index(advertised) < engine.index(marker) < engine.index(settled) and
            native_wall(settled)[0] >= deadline - 25 and native_wall(settled)[1] < platform_deadline,
            'native failed answer must settle at the effective boundary before platform expiry')
    # LCK and CX UUIDs need not match. Additional native actions may only be the
    # later system end; this journal does not independently expose its call UUID.
    action_ids = set(); later_ends = []
    for transaction in delivered:
        ids = transaction.get('actionIDs')
        require(isinstance(ids, list) and len(ids) == 1 and isinstance(ids[0], str) and ids[0] and
                ids[0] not in action_ids and transaction.get('provider') == answer['provider'], 'unknown/reused fail native action')
        action_ids.add(ids[0])
        if transaction is answer: continue
        require(transaction.get('kinds') == ['CXEndCallAction'] and transaction.get('origin') == 'system' and
                transaction.get('requestID') == 'system' and engine.index(transaction) > engine.index(settled),
                'unrelated action in fail contract')
        later_ends.append(transaction)
    for event in ('action.deadline', 'action.settled', 'action.timeout'):
        values = [r for r in engine if r['event'] == event]
        require(len({r.get('actionID') for r in values}) == len(values) and
                all(r.get('actionID') in action_ids for r in values), 'unbound/duplicate native fail event')
        if event == 'action.deadline':
            require({r['actionID'] for r in values} == action_ids and
                    all(r.get('provider') == answer['provider'] and r.get('apiKind') == 'livecommunicationkit'
                        for r in values), 'missing/mixed native fail deadlines')
        for value in values:
            transaction = next(r for r in delivered if r['actionIDs'] == [value['actionID']])
            require(engine.index(transaction) < engine.index(value) and
                    (event == 'action.deadline' or
                     (engine.index(one(engine, 'action.deadline', actionID=value['actionID'])) < engine.index(value) and
                      value.get('origin') == transaction['origin'])) and
                    (event != 'action.timeout' or value['actionID'] != native_id), 'invalid fail action event order/origin/timeout')

    diagnostics = [r for r in rows if r['event'] == 'contract.actionDiagnostic' and r.get('actionID') == action_id]
    phases = ('policy-fire', 'platform-settle-enter', 'platform-settle-return', 'pending-state-published')
    require([r.get('phase') for r in diagnostics] == list(phases), 'missing/reordered fail diagnostics')
    sdk_platform = diagnostics[0].get('platformDeadlineMS')
    require(type(sdk_platform) is int and abs(sdk_platform - deadline - 1500) <= 1,
            'fail diagnostic safety margin differs')
    for index, diagnostic in enumerate(diagnostics):
        sample = diagnostic.get('sdkWallTimeMS')
        require(type(sample) is int and diagnostic.get('effectiveDeadlineMS') == deadline and
                diagnostic.get('platformDeadlineMS') == sdk_platform and
                deadline <= sample < min(sdk_platform, platform_deadline) and
                0 <= diagnostic['wallTimeMS'] - sample <= 25 and
                diagnostic.get('entryPending') is (index == 0) and type(diagnostic.get('platformPending')) is bool and
                (index > 1 or diagnostic['platformPending'] is True), 'invalid/late fail diagnostic')
    policy, enter, _, published = diagnostics
    require(rows.index(pending) < rows.index(policy) and
            all(a['sdkWallTimeMS'] <= b['sdkWallTimeMS'] for a, b in zip(diagnostics, diagnostics[1:])) and
            native_wall(settled)[0] >= enter['sdkWallTimeMS'] - 25 and
            native_wall(settled)[1] < sdk_platform, 'fail settlement/diagnostic clock binding or SDK expiry differs')
    snapshots = [r for r in rows if r['event'] == 'contract.snapshot']
    require(snapshots and all(type(r.get('revision')) is int for r in snapshots) and
            all(a['revision'] < b['revision'] for a, b in zip(snapshots, snapshots[1:])), 'invalid fail snapshot revision')
    target_pending = []; cleared = []
    for snapshot in snapshots:
        calls = snapshot.get('conversations')
        require(isinstance(calls, list) and len(calls) <= 1, 'duplicate fail conversation projection')
        for value in calls:
            require(isinstance(value, dict) and value.get('id') == call and value.get('hasConnectedAt') is False and
                    value.get('status') in ('ringing', 'connecting', 'ending', 'ended'), 'unbound/connected fail conversation')
            action = value.get('pending')
            if action is not None:
                require(isinstance(action, dict), 'invalid fail pending projection')
                if action.get('id') == action_id:
                    require(action == dict(id=action_id, kind='join', origin='app', deadlineMS=deadline), 'fail pending identity/deadline changed')
                    target_pending.append(snapshot)
                else:
                    require(isinstance(action.get('id'), str) and action['id'] and action.get('kind') == 'end' and
                            action.get('origin') == 'system' and type(action.get('deadlineMS')) is int and
                            later_ends and any(native_wall(end)[0] <= snapshot['wallTimeMS'] + 25 for end in later_ends) and
                            rows.index(snapshot) > rows.index(enter), 'unknown fail pending action')
        if rows.index(snapshot) > rows.index(enter) and not any((v.get('pending') or {}).get('id') == action_id for v in calls):
            cleared.append(snapshot)
    require(target_pending and target_pending[0]['wallTimeMS'] < deadline and
            rows.index(target_pending[-1]) < rows.index(policy) and cleared and
            rows.index(cleared[0]) < rows.index(published), 'fail pending clearing lacks observer publication')
    observation = one(rows, 'contract.failureObservation')
    preceding = [r for r in snapshots if rows.index(r) < rows.index(observation)]
    require(preceding, 'fail observation precedes snapshots')
    observed = preceding[-1]['conversations']
    require(observation.get('conversationID') == call and observation.get('pendingCleared') is True and
            observation.get('conversationPresent') is bool(observed) and
            observation.get('status') == (observed[0]['status'] if observed else 'absent') and
            observation['wallTimeMS'] >= sdk_platform and
            rows.index(published) < rows.index(observation) and
            not any(r['event'] in ('contract.observation', 'contract.endReportRequested', 'contract.reportEnded') for r in rows) and
            one(rows, 'contract.joinResult').get('accepted') is True, 'fabricated fail observation/connection')
    reports = [r for r in engine if r['event'] == 'report.ended']
    result = one(rows, 'contract.result')
    if observed:
        requested = one(rows, 'contract.fixtureCleanupRequested'); reported = one(rows, 'contract.fixtureCleanupReported')
        native_report = one(engine, 'report.ended')
        require(all(r.get('conversationID') == call and r.get('reason') == 'failed' for r in (requested, reported)) and
                rows.index(observation) < rows.index(requested) < rows.index(reported) < rows.index(result) and
                not any(r['event'] == 'contract.fixtureCleanupSkipped' for r in rows) and
                rows.index(snapshots[-1]) > rows.index(requested) and
                native_report.get('callUUID') == call and native_report.get('reason') == 1 and
                engine.index(native_report) > engine.index(settled) and
                native_wall(native_report)[0] >= requested['wallTimeMS'] - 25 and
                native_wall(native_report)[1] <= reported['wallTimeMS'] + 25, 'fabricated/unbound fixture cleanup')
    else:
        skipped = one(rows, 'contract.fixtureCleanupSkipped')
        require(skipped.get('conversationID') == call and rows.index(observation) < rows.index(skipped) < rows.index(result) and
                not reports and not any(r['event'] in ('contract.fixtureCleanupRequested', 'contract.fixtureCleanupReported') for r in rows),
                'absent fail conversation must skip fixture cleanup')
    require(snapshots[-1]['conversations'] == [] and rows.index(snapshots[-1]) < rows.index(result) and
            all(r.get('uuid') == call and r.get('connected') is False for r in engine if r['event'] == 'call.changed') and
            one(engine, 'provider.created').get('provider') == answer['provider'], 'fail cleanup/state/provider differs')
    return dict(success=True, effectiveDeadlineMS=deadline, sdkPlatformDeadlineMS=sdk_platform, runtimeDeadlineMS=platform_deadline,
                observedStatus=observation['status'], fixtureCleanup='reported' if observed else 'skipped',
                policyAfterEffectiveDeadlineMS=policy['sdkWallTimeMS'] - deadline,
                observationAfterSDKPlatformDeadlineMS=observation['wallTimeMS'] - sdk_platform,
                nativeFailureBeforeSDKPlatformDeadlineMS=sdk_platform - native_wall(settled)[1],
                nativeFailureBeforeRuntimeDeadlineMS=platform_deadline - native_wall(settled)[1])


def validate_build(build, source_root=ROOT):
    info = json.loads((build / 'build-info.json').read_text())
    identity = source_identity(source_root)
    require(info.get('schemaVersion') == 1 and info.get('product') == 'sdk-m1-contracts' and
            info.get('configuration') == 'Debug' and identity['sourceDirty'] is False and
            all(info.get(k) == v for k, v in identity.items()), 'uncommitted or mismatched build source')
    require(info.get('files') == {name: digest(build / name) for name in BUILD_FILES} and
            json.loads((build / 'source-before.json').read_text()) == identity ==
            json.loads((build / 'source-after.json').read_text()), 'build files/source binding differs')
    return info, identity


def validate(build, capture, source_root=ROOT, parent_manifest=None, cases=CASES):
    info, identity = validate_build(build, source_root)
    require(cases and len(set(cases)) == len(cases) and all(case in CASES for case in cases), 'unknown or duplicate contract case')
    first = json.loads((capture / ('sdk-m1-contract-' + cases[0] + '.receipt.json')).read_text())
    manifest = {k: first.get(k) for k in ('runID', 'simulatorID')}
    require(all(isinstance(v, str) and v for v in manifest.values()), 'missing run/simulator identity')
    if parent_manifest is not None:
        parent = json.loads(parent_manifest.read_text())
        require(all(parent.get(k) == v for k, v in manifest.items()) and
                parent.get('sourceCommit') == identity['sourceCommit'], 'contracts differ from the parent SDK run')
    results = {}
    execution_os = None
    processes = set()
    for case in cases:
        name = 'sdk-m1-contract-' + case
        rows, receipt = bound_rows(capture, name, manifest, BUNDLE)
        require(receipt['appPID'] not in processes, 'contracts reused a process')
        processes.add(receipt['appPID'])
        require(receipt.get('appEntryObserved') is True and [r['event'] for r in rows[:2]] == ['app.entry', 'launch'], 'missing app entry')
        require([r.get('sequence') for r in rows] == list(range(1, len(rows) + 1)) and
                all(type(r.get('wallTimeMS')) is int and r['wallTimeMS'] > 0 and
                    r.get('simulatorID') == manifest['simulatorID'] for r in rows) and
                all(a['wallTimeMS'] <= b['wallTimeMS'] for a, b in zip(rows, rows[1:])), 'invalid sequence/wall time')
        launch = one(rows, 'launch')
        require(isinstance(launch.get('simulatorOS'), str) and launch['simulatorOS'], 'missing execution OS')
        if execution_os is None: execution_os = launch['simulatorOS']
        require(launch['simulatorOS'] == execution_os, 'execution OS changed between contracts')
        require(launch.get('mode') == 'sdk-m1-contracts' and launch.get('case') == case and
                launch.get('traceVersion') == 2 and launch.get('runtimeInjectionPresent') is False and
                launch.get('executableSHA256') == info['files']['SDKM1Contracts.app/SDKM1Contracts'], 'launch/build contract differs')
        require(one(rows, 'contract.activation').get('active') is True and
                rows.index(one(rows, 'contract.activation')) < rows.index(one(rows, 'contract.owner')), 'runtime activation order differs')
        journal = capture / (name + '.runtime.jsonl')
        require(receipt.get('journalSHA256') == digest(journal), 'journal hash mismatch')
        engine = [json.loads(line) for line in journal.read_text().splitlines()]
        require(engine and [r.get('sequence') for r in engine] == list(range(1, len(engine) + 1)) and
                all(type(r.get('t_ms')) is int and r['t_ms'] >= 0 for r in engine) and
                all(a['t_ms'] <= b['t_ms'] for a, b in zip(engine, engine[1:])) and
                all(r.get('runID') == manifest['runID'] and r.get('pid') == receipt['appPID'] and
                    r.get('bundleID') == BUNDLE and r.get('profile') == 'ios-27.0' for r in engine), 'mixed runtime journal')
        require(one(engine, 'runtime.installed').get('version') == (source_root / 'VERSION').read_text().strip(), 'runtime version differs')
        if case == 'guard': results[case] = guard_contract(rows, engine)
        elif case == 'unhandled-accept': results[case] = accept_contract(rows, engine, source_root)
        elif case == 'unhandled-fail': results[case] = fail_contract(rows, engine, source_root)
        elif case == 'audio-lifecycle':
            require(re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,2}', execution_os), 'lifecycle: invalid execution OS')
            modern = int(execution_os.split('.')[0]) >= 27
            results[case] = validate_authority(rows, engine) if modern else validate_lifecycle(rows, engine, 'legacy')
        elif case == 'audio-routes': results[case] = routes_contract(rows, engine)
        elif case == 'media-reentrancy': results[case] = reentrancy_contract(rows, engine)
        else: results[case] = duplicate_contract(rows, engine)
        require(one(rows, 'contract.result').get('success') is True and not any(r['event'] == 'contract.error' for r in rows), 'native contract reported failure')
    return dict(success=True, classification='measured native contracts', cases=results,
                simulatorOS=execution_os, source=identity, buildInfoSHA256=digest(build / 'build-info.json'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path); parser.add_argument('capture', type=Path)
    parser.add_argument('--parent-manifest', type=Path)
    parser.add_argument('--case', choices=CASES, help='Run one contract; the default requires the complete suite')
    args = parser.parse_args()
    try: result = validate(args.build, args.capture, parent_manifest=args.parent_manifest,
                           cases=(args.case,) if args.case else CASES)
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as error:
        result = dict(success=False, error=str(error))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['success'] else 1)
