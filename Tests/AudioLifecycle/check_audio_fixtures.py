#!/usr/bin/env python3
"""Test historical ordinary-session rows; these do not qualify current LCK audio."""
from copy import deepcopy
import json
import math
from pathlib import Path
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'docs/research/callkit-simulator-probe/audio-session-lifecycle'
PHASES = ('sync-activate', 'sync-deactivate', 'async-activate', 'async-deactivate')
NAMES = {
    'active': 'AVAudioSessionDidBecomeActiveNotification',
    'inactive': 'AVAudioSessionDidBecomeInactiveNotification',
    'recommended': 'AVAudioSessionResumptionRecommendationNotification',
    'legacy': 'AVAudioSessionInterruptionNotification',
    'route': 'AVAudioSessionRouteChangeNotification',
}
STREAMS = ('raw.notification', 'decoder.lifecycle', 'consumer.lifecycle')
SCALARS = ('kind', 'hasSource', 'source', 'hasInterruptionReason', 'interruptionReason', 'shouldResume')


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def one(rows, event, **fields):
    selected = [r for r in rows if r['event'] == event and all(r.get(k) == v for k, v in fields.items())]
    require(len(selected) == 1, f'expected one {event} {fields}')
    return selected[0]


def load_trace(no_io):
    name = '2026-10-22-iphone13-ios27' + ('-noio' if no_io else '')
    return [json.loads(line) for line in (FIXTURES / name / 'device-trace.jsonl').read_text().splitlines()]


def validate(rows, no_io):
    require(rows and all(type(r) is dict for r in rows), 'nonempty object rows required')
    for index, row in enumerate(rows, 1):
        require(type(row.get('sequence')) is int and row['sequence'] == index and
                type(row.get('t_ms')) is int and row['t_ms'] >= 0 and
                type(row.get('wallTimeMS')) is int and row['wallTimeMS'] > 0 and
                isinstance(row.get('event'), str) and isinstance(row.get('phase'), str), 'invalid row/time types')
    require(all(a['t_ms'] <= b['t_ms'] and a['wallTimeMS'] <= b['wallTimeMS']
                for a, b in zip(rows, rows[1:])), 'nonmonotonic timestamps')
    launch = one(rows, 'launch')
    require(rows[0] is launch and rows[-1]['event'] == 'done' and launch.get('simulator') is False and
            launch.get('osVersion') == '27.0' and launch.get('silentOutputRequested') is (not no_io) and
            type(launch.get('pid')) is int and launch['pid'] > 0 and
            isinstance(launch.get('runID'), str) and str(UUID(launch['runID'])).upper() == launch['runID'],
            'invalid physical launch/mode')
    require(all(r.get('runID') == launch['runID'] and type(r.get('pid')) is int and
                r['pid'] == launch['pid'] and r.get('bundleID') == 'dev.iosvoipsdk.audio-lifecycle-probe' and
                r.get('main') is True for r in rows), 'mixed launch identity')
    installed = one(rows, 'observers.installed')
    sentinels = [one(rows, 'control.privateSentinel' + suffix) for suffix in ('Posted', 'Received', 'Returned')]
    active, configured = one(rows, 'application.activeBeforeAudio'), one(rows, 'session.configured')
    require(installed.get('actualSwiftConsumer') is True and installed.get('independentCDecoder') is True and
            installed.get('rawNotificationNames') == list(NAMES.values()) and active.get('active') is True and
            configured.get('actualSDKConfiguration') is True and
            all(r.get('syntheticPrivateControl') is True and r.get('countsAsAVNotification') is False
                for r in sentinels), 'missing observer/setup controls')
    setup = [installed, *sentinels, active, configured]
    require([r['sequence'] for r in setup] == sorted({r['sequence'] for r in setup}), 'unordered setup')

    streams = []
    for event in STREAMS:
        all_rows = [r for r in rows if r['event'] == event]
        allowed = set(NAMES) if event == 'raw.notification' else {'active', 'inactive', 'recommended'}
        require(all_rows and all(r.get('kind') in allowed for r in all_rows), 'missing/unknown lifecycle stream')
        for row in all_rows:
            require(all(type(row.get(k)) is bool for k in ('hasSource', 'hasInterruptionReason', 'shouldResume')) and
                    all(type(row.get(k)) is int for k in ('source', 'interruptionReason')) and
                    (row['hasSource'] or row['source'] == 0) and
                    (row['hasInterruptionReason'] or row['interruptionReason'] == 0), 'invalid scalar type/absence')
            if event == 'raw.notification':
                require(row.get('notificationName') == NAMES[row['kind']], 'wrong Apple notification name')
                expected = 'AVAudioSessionDeactivationContext' if row['kind'] == 'inactive' else ''
                require(row.get('contextClass') == expected and
                        row['hasSource'] is (row['kind'] == 'inactive') and
                        (row['kind'] != 'inactive' or row['source'] == 1) and
                        row['hasInterruptionReason'] is False and row['shouldResume'] is False,
                        'wrong raw deactivation context/source')
            else:
                require(row.get('contextClass') == '', 'decoded stream must contain scalars only')
        streams.append([r for r in all_rows if r['kind'] in ('active', 'inactive', 'recommended')])
    tagged = [[tuple((type(r.get(k)), r.get(k)) for k in ('phase', *SCALARS)) for r in stream] for stream in streams]
    require(tagged[0] == tagged[1] == tagged[2], 'ordered raw/C/Swift scalar inventories differ')
    # These counts and phases describe these two recordings, not an OS-wide rule.
    expected = [('sync-activate', 'active'), ('sync-deactivate', 'inactive'),
                ('sync-deactivate', 'inactive'), ('async-activate', 'active')]
    expected += [('async-deactivate', 'inactive')] * 2 if no_io else [('cleanup', 'inactive')]
    require([(r['phase'], r['kind']) for r in streams[0]] == expected, 'historical lifecycle inventory differs')

    phases = PHASES if no_io else PHASES[:3]
    previous = configured
    for event in ('operation.requested', 'operation.result', 'operation.observed'):
        require([r['phase'] for r in rows if r['event'] == event] == list(phases), 'operation missing/duplicated')
    for index, phase in enumerate(phases):
        request, result, observed = [one(rows, event, phase=phase) for event in
                                     ('operation.requested', 'operation.result', 'operation.observed')]
        require(type(request.get('index')) is int and request['index'] == index and
                request.get('applicationActive') is True and
                previous['sequence'] < request['sequence'] < result['sequence'] < observed['sequence'],
                'unordered operation')
        require(all(result.get(k) is True for k in ('sessionAttempted', 'sessionSuccess', 'ioSuccess', 'callAudioQueue')) and
                result.get('callMain') is False and type(result.get('completionMain')) is bool and
                all(result.get(k) == '' for k in ('sessionErrorDomain', 'ioErrorDomain')) and
                all(type(result.get(k)) is int and result[k] == 0 for k in ('sessionErrorCode', 'ioErrorCode')),
                'operation failed or ran on wrong queue')
        elapsed = observed.get('observationElapsedMS')
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 4990 and
                observed['t_ms'] - result['t_ms'] >= 4990 and
                type(observed.get('mainHeartbeatTicks')) is int and observed['mainHeartbeatTicks'] >= 10,
                'observation window/heartbeat missing')
        previous = observed
    cleanup, done = one(rows, 'fixture.cleanup'), one(rows, 'done')
    require(previous['sequence'] < cleanup['sequence'] < done['sequence'] and
            cleanup.get('callAudioQueue') is True and cleanup.get('callMain') is False and
            cleanup.get('sessionSuccess') is True and cleanup.get('engineRunning') is False, 'cleanup differs')
    io_rows = [r for r in rows if any(k in r for k in ('engineRunning', 'renderFrames', 'renderCallbacks'))]
    require(io_rows == [r for r in rows if r['event'] in ('operation.result', 'operation.observed', 'fixture.cleanup')] and
            len(io_rows) == len(phases) * 2 + 1 and
            all(type(r.get('engineRunning')) is bool and all(type(r.get(k)) is int and r[k] >= 0
                for k in ('renderFrames', 'renderCallbacks')) for r in io_rows), 'invalid I/O snapshots')
    if no_io:
        completed = one(rows, 'experiment.completed')
        require(not any(r['event'] == 'experiment.failed' for r in rows) and
                cleanup['sequence'] < completed['sequence'] < done['sequence'] and
                type(completed.get('operations')) is int and completed['operations'] == 4 and
                completed.get('ordinarySessionOnly') is True and completed.get('notLCKInterruptionAcceptance') is True and
                all(r['engineRunning'] is False and r['renderFrames'] == r['renderCallbacks'] == 0 for r in io_rows),
                'no-I/O completion or nine zero-I/O snapshots differ')
    else:
        failed = one(rows, 'experiment.failed')
        sync_initial, sync_observed = [one(rows, event, phase='sync-activate') for event in
                                       ('operation.result', 'operation.observed')]
        initial, observed = [one(rows, event, phase='async-activate') for event in ('operation.result', 'operation.observed')]
        require(not any(r['event'] == 'experiment.completed' for r in rows) and
                previous['sequence'] < failed['sequence'] < cleanup['sequence'] and failed.get('errorType') == 'ProbeFailure' and
                sync_initial['engineRunning'] is True and sync_observed['engineRunning'] is True and
                sync_observed['renderFrames'] > sync_initial['renderFrames'] and
                sync_observed['renderCallbacks'] > sync_initial['renderCallbacks'] and
                initial['engineRunning'] is True and observed['engineRunning'] is False and
                initial['renderFrames'] == observed['renderFrames'] and
                initial['renderCallbacks'] == observed['renderCallbacks'] and
                cleanup.get('cleanupDeactivationRequested') is True, 'ordinary RED outcome was erased')


class AudioFixtureTests(unittest.TestCase):
    def test_recorded_outcomes(self):
        for no_io in (False, True):
            with self.subTest(no_io=no_io):
                validate(load_trace(no_io), no_io)

    def test_negative_mutations(self):
        for no_io in (False, True):
            mutations = {
                'missing stream': lambda rows: rows.__setitem__(slice(None), [r for r in rows if r['event'] != 'consumer.lifecycle']),
                'wrong name': lambda rows: one(rows, 'raw.notification', phase='sync-activate', kind='active').update(notificationName='wrong'),
                'wrong context': lambda rows: next(r for r in rows if r['event'] == 'raw.notification' and r['kind'] == 'inactive').update(contextClass='wrong'),
                'wrong scalar': lambda rows: next(r for r in rows if r['event'] == 'decoder.lifecycle').update(source=2),
                'raw/C/Swift disagreement': lambda rows: next(r for r in rows if r['event'] == 'decoder.lifecycle').update(shouldResume=True),
                'boolean as integer': lambda rows: next(r for r in rows if r['event'] == 'decoder.lifecycle').update(source=False),
                'integer as boolean': lambda rows: next(r for r in rows if r['event'] == 'decoder.lifecycle').update(hasSource=0),
                'wrong stream order': lambda rows: next(r for r in rows if r['event'] == 'decoder.lifecycle').update(phase='sync-deactivate'),
                'mixed PID': lambda rows: rows[-1].update(pid=1),
                'wrong timestamp type': lambda rows: rows[-1].update(t_ms=True),
                'short window': lambda rows: one(rows, 'operation.observed', phase='sync-activate').update(observationElapsedMS=1),
                'no heartbeat': lambda rows: one(rows, 'operation.observed', phase='sync-activate').update(mainHeartbeatTicks=0),
            }
            if no_io:
                mutations['fabricated completion'] = lambda rows: one(rows, 'experiment.completed').update(operations=True)
                mutations['nonzero I/O'] = lambda rows: one(rows, 'operation.result', phase='sync-activate').update(renderFrames=1)
            else:
                mutations['fabricated completion'] = lambda rows: one(rows, 'experiment.failed').update(event='experiment.completed', operations=4)
                mutations['invented frame progress'] = lambda rows: one(rows, 'operation.observed', phase='async-activate').update(renderFrames=240673)
            for name, mutate in mutations.items():
                with self.subTest(no_io=no_io, mutation=name):
                    rows = deepcopy(load_trace(no_io))
                    mutate(rows)
                    for index, row in enumerate(rows, 1):
                        row['sequence'] = index
                    with self.assertRaises(AssertionError):
                        validate(rows, no_io)


if __name__ == '__main__':
    unittest.main()
