#!/usr/bin/env python3
"""Tap the separate inspector, then consume its runtime journal and UI screenshots."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Tests/RuntimeEvents'))
from run import capture_diagnostics, fixture_session, latest_runtime, launch_fixture, record_environment, remove_simulators, run

BUNDLE = 'dev.callkitsim.eventfixture'
INSPECTOR = 'dev.callkitsim.inspector'


class ProbeReader:
    """Accept only fresh, process-pinned projections written by the Inspector."""
    def __init__(self, path, nonce, session, diagnostic_path=None, resume_log=None):
        self.path, self.nonce, self.session = path, nonce, session
        self.diagnostic_path = diagnostic_path
        self.resume_log, self.resume_raw = resume_log, None
        self.pid = self.started = None
        self.cursor = 0
        self.online = False
        self.reason = 'receipt not published'

    def read(self, resume_after=None):
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return None
        if self.diagnostic_path is not None:
            try:
                self.diagnostic_path.write_text(raw)
            except OSError as error:
                print('Cannot save Inspector receipt diagnostic: ' + str(error), file=sys.stderr)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            self.reason = 'Inspector receipt publication is incomplete'
            return None
        if value.get('nonce') != self.nonce:
            if self.pid is not None:
                raise RuntimeError('Inspector nonce changed')
            self.reason = 'receipt nonce does not match this launch'
            return None
        now = time.time() * 1000
        pid = value['inspectorPID']
        if type(pid) is not int or pid <= 0 or self.pid not in (None, pid):
            raise RuntimeError('Inspector process changed or has invalid identity')
        if self.started not in (None, value['startedWallTimeMS']):
            raise RuntimeError('Inspector startup identity changed')
        self.pid, self.started = pid, value['startedWallTimeMS']
        session = value.get('session')
        if session is None and not self.online:
            self.reason = value.get('error') or 'Inspector has not connected'
            return None
        keys = ('schemaVersion', 'runID', 'pid', 'bundleID', 'profile')
        if not session or any(session.get(key) != self.session[key] for key in keys):
            raise RuntimeError('Inspector target session changed')
        cursor = value['cursor']
        if type(cursor) is not int or cursor < self.cursor:
            raise RuntimeError('Inspector cursor regressed')
        rows = value['timeline']
        if [row['sequence'] for row in rows] != list(range(cursor - len(rows) + 1, cursor + 1)):
            raise RuntimeError('Inspector timeline is inconsistent with its cursor')
        if any(any(row.get(key) != self.session[key] for key in keys[:-1]) for row in rows):
            raise RuntimeError('Inspector timeline identity changed')
        if not -1000 <= now - value['startedWallTimeMS'] <= 25000:
            raise RuntimeError('Inspector background budget expired')
        if resume_after is not None:
            if self.resume_log is not None and raw != self.resume_raw:
                try:
                    with self.resume_log.open('a') as output:
                        output.write(json.dumps(dict(resumeAfterMS=resume_after, receipt=value)) + '\n')
                except OSError as error:
                    print('Cannot save Inspector resume diagnostic: ' + str(error), file=sys.stderr)
                self.resume_raw = raw
            if value['wallTimeMS'] < resume_after or not value['online'] or value.get('error'):
                self.reason = 'Inspector resuming: ' + str(value.get('error') or 'awaiting a new receipt')
                return None
        if not -1000 <= now - value['wallTimeMS'] <= 5000:
            raise RuntimeError('Inspector receipt is stale')
        if self.online and (not value['online'] or value.get('error')):
            raise RuntimeError('Inspector disconnected: ' + str(value.get('error')))
        self.cursor = cursor
        self.online = self.online or value['online']
        self.reason = value.get('error') or f'Inspector has not reached the requested state (cursor {cursor})'
        return value

    def wait(self, predicate, timeout=5, deadline=None, resume_after=None):
        stop = min(time.monotonic() + timeout, deadline if deadline is not None else float('inf'))
        if resume_after is not None:
            self.resume_raw = None
        while time.monotonic() < stop:
            value = self.read(resume_after=resume_after)
            if value is not None and value['online']:
                resume_after = None
            if value is not None and predicate(value):
                return value
            time.sleep(0.05)
        if deadline is not None:
            remaining_budget(deadline)
        raise RuntimeError('Inspector projection timed out: ' + self.reason)


def remaining_budget(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError('Inspector consumer background budget expired')
    return remaining


def activate_existing(device, bundle, pid, deadline):
    # Foregrounding shares the consumer lifetime, including hosted launch latency.
    output = run('xcrun', 'simctl', 'launch', device, bundle, timeout=min(10, remaining_budget(deadline)))
    identities = [line.split(':', 1)[1].strip() for line in output.splitlines() if line.startswith(bundle + ':')]
    if identities != [str(pid)]:
        raise RuntimeError(f'Foreground activation PID changed for {bundle}: expected {pid}, got {output!r}')


def read_runtime_journal(channel, session):
    if json.loads((channel / 'session.json').read_text()) != session:
        raise RuntimeError('Runtime session changed during foreground control')
    data = (channel / session['runID'] / 'events.jsonl').read_bytes()
    complete = data.rsplit(b'\n', 1)[0].split(b'\n') if b'\n' in data else []
    rows = [json.loads(line) for line in complete if line]
    for sequence, row in enumerate(rows, 1):
        if row['sequence'] != sequence or any(row.get(key) != session[key] for key in ('schemaVersion', 'runID', 'pid', 'bundleID', 'profile')):
            raise RuntimeError('Runtime journal identity/order changed')
    return rows


def wait_runtime(predicate, channel, session, deadline, timeout=5):
    stop = min(time.monotonic() + timeout, deadline)
    while time.monotonic() < stop:
        rows = read_runtime_journal(channel, session)
        remaining_budget(deadline)
        if predicate(rows):
            return rows
        time.sleep(0.05)
    remaining_budget(deadline)
    raise RuntimeError('Foreground runtime journal did not reach the required state')


def verify_probe_journal(receipts, rows, session):
    indexed = {row['sequence']: row for row in rows}
    assert rows and all(all(row[key] == session[key] for key in ('runID', 'pid', 'bundleID')) for row in rows)
    for receipt in receipts:
        for row in receipt['timeline']:
            original = indexed.get(row['sequence'], {})
            assert all(original.get(key) == value for key, value in row.items()), 'Inspector timeline differs from runtime journal'
        projected = {row['uuid']: row for row in rows if row['sequence'] <= receipt['cursor'] and row['event'] == 'call.changed'}
        assert {row['uuid'] for row in receipt['conversations']} == set(projected)
        for call in receipt['conversations']:
            assert all(projected[call['uuid']].get(key) == value for key, value in call.items())
        if 'timingReferences' in receipt:
            verify_timing_reference_journal(receipt, rows)


def journal_mute_value(action, rows):
    """Resolve native mute parameters without trusting the Inspector's projection."""
    identity = ('schemaVersion', 'runID', 'pid', 'bundleID')
    if not all(type(action.get(key)) is str and action[key] for key in ('transactionID', 'provider', 'actionID', 'callUUID')):
        return None
    transactions = [row for row in rows if row['event'] == 'transaction.delivered' and
                    all(key in row and key in action and row[key] == action[key] for key in identity) and
                    row.get('transactionID') == action['transactionID']]
    direct = action.get('muted')
    if not transactions:
        return direct if type(direct) is bool else None
    if len(transactions) != 1:
        return None
    transaction = transactions[0]
    if transaction.get('provider') != action['provider'] or transaction.get('origin') != action.get('origin'):
        return None
    if (type(transaction.get('sequence')) is not int or type(action.get('sequence')) is not int or
            transaction['sequence'] >= action['sequence'] or
            any(type(row.get('t_ms')) not in (int, float) or not math.isfinite(row['t_ms']) for row in (transaction, action)) or
            transaction['t_ms'] > action['t_ms']):
        return None
    actions, calls, kinds, parameters = [transaction.get(key) for key in ('actionIDs', 'callUUIDs', 'kinds', 'parameters')]
    if (not all(type(value) is list for value in (actions, calls, kinds, parameters)) or not actions or
            not len(actions) == len(calls) == len(kinds) == len(parameters) or
            not all(type(value) is str and value for value in actions + calls + kinds) or
            len(set(actions)) != len(actions) or not all(type(value) is dict for value in parameters) or
            action['actionID'] not in actions):
        return None
    index = actions.index(action['actionID'])
    value = parameters[index].get('muted')
    if (action.get('kind') != 'mute' or kinds[index] != 'CXSetMutedCallAction' or calls[index] != action['callUUID'] or
            type(value) is not bool or direct is not None and (type(direct) is not bool or direct != value)):
        return None
    return value


def verify_timing_reference_journal(receipt, rows, required=()):
    """Require measured values to cite retained, correlated original journal rows."""
    references = {'pushReport': '≈190 ms', 'answerAudio': '≈236 ms',
                  'muteEcho': '8–17 ms after fulfillment', 'unreportedPush': '≈4.4 s'}
    values = receipt['timingReferences']
    assert len(values) == 4 and {value['id'] for value in values} == set(references)
    indexed = {row['sequence']: row for row in rows}
    retained = {row['sequence'] for row in receipt['timeline']}
    originals = [indexed[sequence] for sequence in sorted(retained)]
    for value in values:
        name = value['id']
        assert value['deviceValue'] == references[name]
        assert 'LCK · iPhone 13 · iOS 27.0' in value['deviceProvenance']
        assert value['sourcePath'] == 'docs/research/callkit-simulator-probe/livecommunicationkit/push/README.md'
        actual = value.get('actualMS')
        if actual is None:
            assert name not in required, f'{name} actual is unavailable'
            assert value.get('startSequence') is None and value.get('endSequence') is None
            continue
        assert type(actual) in (int, float) and math.isfinite(actual) and actual >= 0
        start_sequence, end_sequence = value['startSequence'], value['endSequence']
        assert start_sequence in retained and end_sequence in retained and start_sequence < end_sequence
        start, end = indexed[start_sequence], indexed[end_sequence]
        assert all(start[key] == end[key] for key in ('schemaVersion', 'runID', 'pid', 'bundleID'))
        elapsed = end['t_ms'] - start['t_ms']
        assert math.isfinite(elapsed) and elapsed >= 0 and math.isclose(actual, elapsed, rel_tol=0, abs_tol=1e-6)
        if name in ('pushReport', 'unreportedPush'):
            candidates = [row for row in originals if row['event'] == 'push.received']
            assert candidates[-1]['sequence'] == start_sequence
            assert start.get('pushID') and start['pushID'] == end.get('pushID')
            expected = 'report.completed' if name == 'pushReport' else 'diagnostic.pushReportExpired'
            assert end['event'] == expected
            if name == 'unreportedPush':
                assert start.get('mustReport') is True
            else:
                assert type(end.get('error')) is int and end['error'] == 0
        elif name == 'answerAudio':
            candidates = [row for row in originals if row['event'] == 'action.deadline' and row.get('kind') == 'answer']
            assert candidates[-1]['sequence'] == start_sequence
            assert start.get('callUUID') and start.get('actionID') and end['event'] == 'audio.activated'
            label = {'callkit': 'CallKit answer', 'livecommunicationkit': 'LCK join'}.get(start.get('apiKind'))
            assert label and value['actualLabel'] == f'Simulator · {label} delivered → audio.activated'
            between = [row for row in originals if start_sequence < row['sequence'] < end_sequence]
            assert any(row['event'] == 'action.settled' and row.get('kind') == 'answer' and row.get('fulfilled') is True and
                       all(row.get(key) == start.get(key) for key in ('actionID', 'callUUID', 'apiKind')) for row in between)
            assert not any(row['event'] in ('action.deadline', 'action.settled') and
                           row.get('kind') in ('start', 'hold', 'end') for row in between)
            assert not any(row['event'] in ('action.deadline', 'action.settled') and row.get('kind') == 'answer' and
                           row.get('actionID') != start.get('actionID') for row in between)
        else:
            candidates = [row for row in originals if row['event'] == 'action.settled' and
                          row.get('kind') == 'mute' and row.get('origin') == 'application']
            assert candidates[-1]['sequence'] == start_sequence and start.get('fulfilled') is True
            assert end['event'] == 'action.deadline' and end.get('kind') == 'mute' and end.get('origin') == 'muteEcho'
            assert start.get('callUUID') and start['callUUID'] == end.get('callUUID')
            assert all(row.get(key) for row in (start, end) for key in ('actionID', 'transactionID'))
            identity = ('schemaVersion', 'runID', 'pid', 'bundleID')
            same_runtime = [row for row in originals if all(row.get(key) == start.get(key) for key in identity)]
            assert not any(row['sequence'] < end_sequence and row['event'] == 'action.deadline' and
                           row.get('actionID') == end['actionID'] for row in same_runtime), 'Echo action ID was already observed'
            assert not any(row['sequence'] < start_sequence and row['event'] == 'action.settled' and
                           row.get('fulfilled') is True and row.get('actionID') == start['actionID'] for row in same_runtime), 'Fulfillment action ID was already settled'
            muted = journal_mute_value(start, originals)
            assert type(muted) is bool and muted == journal_mute_value(end, originals)
            assert start.get('apiKind') in ('callkit', 'livecommunicationkit') and start['apiKind'] == end.get('apiKind')
            assert start.get('provider') and start['provider'] == end.get('provider')
            assert originals[0]['sequence'] == 1, 'Truncated history cannot establish outstanding mute obligations'
            mute_key = ('schemaVersion', 'runID', 'pid', 'bundleID', 'callUUID', 'apiKind', 'provider')
            outstanding, fulfilled_ids, delivery_ids = 0, set(), set()
            for row in same_runtime:
                if row['sequence'] >= end_sequence:
                    continue
                first_delivery = False
                if row['event'] == 'action.deadline' and type(row.get('actionID')) is str and row['actionID']:
                    first_delivery = row['actionID'] not in delivery_ids
                    delivery_ids.add(row['actionID'])
                if row.get('kind') != 'mute' or not all(row.get(key) == start.get(key) for key in mute_key):
                    continue
                fulfillment = row['event'] == 'action.settled' and row.get('origin') == 'application' and row.get('fulfilled') is True
                echo = row['event'] == 'action.deadline' and row.get('origin') == 'muteEcho'
                if not (fulfillment or echo):
                    continue
                resolved = journal_mute_value(row, originals)
                assert type(resolved) is bool, 'Unknown mute value leaves possible older obligations'
                if resolved != muted:
                    continue
                if row['sequence'] < start_sequence:
                    if fulfillment:
                        assert row.get('actionID') and row.get('transactionID')
                        if row['actionID'] not in fulfilled_ids:
                            fulfilled_ids.add(row['actionID'])
                            outstanding += 1
                    elif echo and first_delivery and row.get('transactionID'):
                        outstanding = max(0, outstanding - 1)
                elif start_sequence < row['sequence'] < end_sequence and fulfillment:
                    assert row.get('actionID') == start.get('actionID'), 'Competing app fulfillment makes mute echo ambiguous'
            assert outstanding == 0, 'Older outstanding app fulfillments make mute echo ambiguous'


def journal_journey(common, target, nonce, session, out, deadline, channel=None):
    """Drive controls and require the foreground Inspector's consumed projections."""
    container = Path(run('xcrun', 'simctl', 'get_app_container', target, INSPECTOR, 'data',
                         timeout=min(10, remaining_budget(deadline))))
    probe = ProbeReader(container / 'Documents/cksim-inspector-results.json', nonce, session,
                        diagnostic_path=out / 'inspector-latest.json', resume_log=out / 'inspector-resumes.jsonl')
    ready = probe.wait(lambda row: row['online'], timeout=10, deadline=deadline)
    deadline = min(deadline, time.monotonic() + 25 - (time.time() * 1000 - probe.started) / 1000)
    remaining_budget(deadline)
    assert not ready.get('incoming') and not ready['conversations'], 'fixture must start without a call'
    (out / 'inspector-online.json').write_text(json.dumps(ready))
    def command(*parts):
        budget = remaining_budget(deadline)
        return run(*common, '--timeout', str(min(5, budget)), *parts, timeout=min(8, budget))
    phases = []
    def foreground(bundle, pid):
        started = time.time() * 1000
        activate_existing(target, bundle, pid, deadline)
        if json.loads((channel / 'session.json').read_text()) != session:
            raise RuntimeError('Runtime session changed during foreground control')
        phases.append(dict(bundleID=bundle, pid=pid, startedWallTimeMS=started, finishedWallTimeMS=time.time() * 1000))
        (out / 'foreground-phases.json').write_text(json.dumps(phases))
        return started
    payload = out / 'incoming.json'
    payload.write_text(json.dumps(dict(operation='incoming')))
    foreground(session['bundleID'], session['pid'])
    assert json.loads(command('push', '--payload-file', payload, '--must-report', 'true'))['success']
    ringing = wait_runtime(lambda rows: any(row['event'] == 'call.changed' and row.get('ringing') for row in rows),
                           channel, session, deadline)
    native_call = next(row for row in reversed(ringing) if row['event'] == 'call.changed' and row.get('ringing'))
    resumed = foreground(INSPECTOR, probe.pid)
    incoming = probe.wait(lambda row: (row.get('incoming') or {}).get('uuid') == native_call['uuid'],
                          deadline=deadline, resume_after=resumed)
    call = incoming['incoming']
    identifier = str(uuid.UUID(call['uuid']))
    assert call.get('ringing') and not call.get('ended') and call.get('callerName') == 'Alex Morgan'
    (out / 'inspector-incoming.json').write_text(json.dumps(incoming))
    foreground(session['bundleID'], session['pid'])
    response = json.loads(command('--command-receipt', out / 'answer-control.json', 'answer', identifier))
    assert response['success']
    action_ids = set(response['result']['actionIDs'])
    assert action_ids
    def settled(rows):
        actions = [row for row in rows if row.get('actionID') in action_ids and row['event'] == 'action.settled']
        return ({row.get('actionID') for row in actions} == action_ids and
                all(row.get('fulfilled') and row.get('origin') == 'system' and row.get('kind') == 'answer' for row in actions) and
                any(row['event'] == 'audio.activated' for row in rows))
    wait_runtime(settled, channel, session, deadline)
    resumed = foreground(INSPECTOR, probe.pid)
    def answered(row):
        current = next((value for value in row['conversations'] if value.get('uuid') == call['uuid']), {})
        settled = [value for value in row['timeline'] if value.get('actionID') in action_ids and value['event'] == 'action.settled']
        return (current.get('connected') is True and row.get('incoming') is None and row['audioActive'] and
                not any(value.get('actionID') in action_ids for value in row['pending']) and
                {value.get('actionID') for value in settled} == action_ids and
                any(value['event'] == 'audio.activated' for value in row['timeline']))
    connected = probe.wait(answered, deadline=deadline, resume_after=resumed)
    assert connected['cursor'] > incoming['cursor']
    (out / 'inspector-answered.json').write_text(json.dumps(connected))
    captured = run(*common, 'watch', '--json', '--duration', '0.3', timeout=min(5, remaining_budget(deadline)))
    (out / 'events.jsonl').write_text(captured + '\n')
    rows = [json.loads(line) for line in captured.splitlines()]
    verify_probe_journal((incoming, connected), rows, session)
    verify_timing_reference_journal(connected, rows, required=('pushReport', 'answerAudio'))
    answers = [row for row in rows if row.get('actionID') in action_ids and row['event'] == 'action.settled']
    assert len(answers) == 1 and answers[0]['kind'] == 'answer' and answers[0]['origin'] == 'system' and answers[0]['fulfilled']
    assert math.isfinite(answers[0]['latencyMS']) and 0 <= answers[0]['latencyMS'] < answers[0]['deadlineSeconds'] * 1000
    presentation = next(row for row in connected['timelinePresentation'] if row['sequence'] == answers[0]['sequence'])
    assert (presentation['title'] == 'Answer fulfilled' and 'system' in presentation['detail'] and
            'profile budget' in presentation['detail'] and ' ms' in presentation['detail'])
    assert time.time() * 1000 - probe.started <= 25000, 'shared fixture background budget exceeded'
    remaining_budget(deadline)
    try:
        run('xcrun', 'simctl', 'io', target, 'screenshot', out / 'inspector-consumer.png', timeout=3)
    except (RuntimeError, OSError) as error:
        try:
            (out / 'screenshot-error.txt').write_text(str(error))
        except OSError:
            pass
    return dict(success=True, driver='journal', uiConsumer=True, answerLatencyMS=answers[0]['latencyMS'])


# Provided display: four 60s simctl calls, a 180s cold launch, and the status reply.
def wait_for_inspector(process, log, timeout=480, consumer_budget=None):
    deadline = time.monotonic() + timeout
    consumer_deadline = None
    while True:
        if process.poll() is not None:
            raise RuntimeError('Inspector exited during startup: ' + log.read_text())
        output = log.read_text()
        if consumer_budget is not None and 'Inspector: launching app on display simulator.' in output:
            if consumer_deadline is None:
                consumer_deadline = time.monotonic() + consumer_budget
            remaining_budget(consumer_deadline)
        if 'Inspector on ' in output:
            if consumer_budget is not None and consumer_deadline is None:
                raise RuntimeError('Inspector shared launch boundary was not observed')
            return consumer_deadline
        if time.monotonic() >= deadline:
            raise RuntimeError('Inspector launch timed out: ' + output)
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--inspector', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--target-simulator')
    parser.add_argument('--fixture-installed', action='store_true',
                        help='Reuse the fixture installed by native acceptance on the supplied target')
    parser.add_argument('--display-simulator')
    parser.add_argument('--runtime', help='Require this exact available simulator runtime identifier')
    parser.add_argument('--driver', choices=('xctest', 'journal'), default='xctest')
    args = parser.parse_args()
    if args.fixture_installed and not args.target_simulator:
        parser.error('--fixture-installed requires an explicit --target-simulator')
    if args.driver == 'journal' and args.display_simulator and args.display_simulator != args.target_simulator:
        parser.error('The journal driver uses its target simulator as the Inspector display')
    out = args.out.resolve()
    out.mkdir(parents=True)
    project = ROOT / 'Tests/Inspector/UITests.xcodeproj'
    build = out / 'ui-build'
    if args.driver == 'xctest':
        with (out / 'ui-build.log').open('w') as log:
            subprocess.run(['xcodebuild','build-for-testing','-project',str(project),'-scheme','InspectorUITests',
                            '-destination','generic/platform=iOS Simulator','-derivedDataPath',str(build),
                            'CODE_SIGNING_ALLOWED=NO'], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
    runtimes = json.loads(run('xcrun','simctl','list','runtimes','-j'))['runtimes']
    runtime = latest_runtime(runtimes, args.runtime)
    record_environment(out, runtime)
    print('Selected simulator runtime: ' + runtime, flush=True)
    owned = []
    bridge = tests = None
    target = display = None
    channel = None
    def device(supplied, name):
        if supplied:
            return supplied
        value = run('xcrun','simctl','create',name + str(uuid.uuid4())[:8],
                    'com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro',runtime)
        owned.append(value)
        run('xcrun','simctl','boot',value)
        run('xcrun','simctl','bootstatus',value,'-b',timeout=300)
        return value
    try:
        target = device(args.target_simulator, 'M4 Target ')
        print('Starting a fresh fixture session on the target simulator.', flush=True)
        channel, run_id = launch_fixture(target, args.fixture, install=not args.fixture_installed)
        session = fixture_session(channel, run_id)
        assert session.get('controlTransport') == 'directory-watch-v1', 'fixture must support spawn-free mailbox controls'
        common = [str(ROOT/'Tools/cksim'),'--simulator',target,'--bundle',BUNDLE,
                  '--runtime-dir',str(args.package.resolve()), '--app-container', str(channel.parent.parent)]
        journal = args.driver == 'journal'
        nonce = str(uuid.uuid4()) if journal else None
        display = target if journal else device(args.display_simulator, 'M4 Inspector ')
        bridge_command = common + ['inspect','--app',str(args.inspector.resolve()), '--inspector-simulator',display]
        if journal:
            bridge_command.append('--allow-shared-simulator')
        with (out / 'bridge.log').open('w') as log:
            bridge = subprocess.Popen(bridge_command, stdout=log, stderr=subprocess.STDOUT,
                                      env=dict(os.environ, SIMCTL_CHILD_CKSIM_INSPECTOR_PROBE_NONCE=nonce) if journal else None)
        consumer_deadline = wait_for_inspector(bridge, out/'bridge.log', consumer_budget=25 if journal else None)
        if journal:
            print(json.dumps(journal_journey(common, target, nonce, session, out, consumer_deadline, channel)))
            return
        with (out / 'ui-test.log').open('w') as log:
            tests = subprocess.Popen(['xcodebuild','test-without-building','-project',str(project),'-scheme','InspectorUITests',
                                      '-destination','platform=iOS Simulator,id='+display,'-derivedDataPath',str(build),
                                      '-resultBundlePath',str(out/'inspector.xcresult'),'CODE_SIGNING_ALLOWED=NO',
                                      '-parallel-testing-enabled','NO','-collect-test-diagnostics','never'], stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and tests.poll() is None:
            started = next((line for line in (out/'ui-test.log').read_text().splitlines()
                            if 'Test Case ' in line and 'testAnswerFromInspector' in line and 'started.' in line), None)
            if started:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('UI test did not start: ' + (out/'ui-test.log').read_text()[-2000:])
        payload = out / 'incoming.json'
        payload.write_text(json.dumps(dict(operation='incoming')))
        timing = dict(testCaseStartLine=started, pushCommandStartedAtMS=int(time.time() * 1000))
        (out/'stimulus-timing.json').write_text(json.dumps(timing))
        run(*common,'push','--payload-file',payload,'--must-report','true')
        timing['pushCommandFinishedAtMS'] = int(time.time() * 1000)
        (out/'stimulus-timing.json').write_text(json.dumps(timing))
        code = tests.wait(timeout=300)
        captured = run(*common, 'watch','--json','--duration','0.3')
        (out/'events.jsonl').write_text(captured+'\n')
        rows = [json.loads(line) for line in captured.splitlines()]
        assert code == 0, (out/'ui-test.log').read_text()[-4000:]
        assert all(all(row[k] == session[k] for k in ('runID', 'pid', 'bundleID')) for row in rows)
        answers = [r for r in rows if r['event']=='action.settled' and r['kind']=='answer']
        assert len(answers)==1 and answers[0]['origin']=='system' and answers[0]['fulfilled']
        assert any(r['event']=='audio.activated' for r in rows)
        run('xcrun','xcresulttool','export','attachments','--path',out/'inspector.xcresult','--output-path',out/'screenshots')
        (out/'environment.txt').write_text(run('xcodebuild','-version')+'\n'+runtime+'\n')
        print(json.dumps(dict(success=True, answerLatencyMS=answers[0]['latencyMS'], screenshots='screenshots/')))
    finally:
        failed = sys.exc_info()[0] is not None
        if target:
            capture_diagnostics(out, target, channel=channel, failed=failed)
        if failed and display:
            capture_diagnostics(out / 'display', display, bundle='dev.callkitsim.inspector', failed=True)
        for process in (tests, bridge):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        remove_simulators(owned)


if __name__ == '__main__':
    main()
