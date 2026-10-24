#!/usr/bin/env python3
"""Genuine LCK report/filter acceptance through the simulator runtime.

App stimuli invoke ConversationManager from the fixture's mailbox. System
stimuli use the runtime mailbox. All observations come from its pinned journal;
choice sets and transaction identifiers are explicitly runtime policy checks.
"""
import argparse
from contextlib import contextmanager
import json
import os
import re
import runpy
from pathlib import Path
import shutil
import sys
import time
import uuid

from run import run
from ci_simulator import create, cleanup, simulator_uuid

BUNDLE = 'dev.callkitsim.lckfixture'
RUNTIME = 'com.apple.CoreSimulator.SimRuntime.iOS-27-0'
PREFIX = 'lck.fixture.'


def verify_barrier(rows, accepted, action_ids):
    terminal = [(index, r) for index, r in enumerate(rows) if r['event'] in (PREFIX + 'settled', PREFIX + 'timedOut') and r.get('actionID') in action_ids]
    assert {r['actionID'] for _, r in terminal} == action_ids, 'missing constituent settlement'
    joined = next(index for index, r in enumerate(rows) if r['event'] == PREFIX + 'changed' and r.get('callUUID') == accepted and r.get('state') == 'joined')
    assert joined > max(index for index, _ in terminal), 'accepted Join happened before every constituent settled'


def verify_timeout(delivered, expired, kind):
    assert 29000 <= delivered['advertisedRemainingMS'] <= 31000, 'LCK must still advertise 30 seconds'
    elapsed = expired['t_ms'] - delivered['t_ms']
    low, high = (63000, 68000) if kind == 'Join' else (5000, 8500)
    assert low <= elapsed <= high, f'{kind} effective timeout {elapsed}ms outside measured scheduling budget'


def snapshot_matches(rows, expected):
    snapshots = [r for r in rows if r['event'] == PREFIX + 'snapshot']
    if not snapshots:
        return False
    actual = {c['callUUID']: c['state'] for c in snapshots[-1]['conversations']}
    return all(actual.get(identifier) == state or state == 'left' and identifier not in actual
               for identifier, state in expected.items())


def verify_unchanged(rows, expected):
    for row in rows:
        if row['event'] == PREFIX + 'changed' and row.get('callUUID') in expected:
            assert row['state'] == expected[row['callUUID']], 'merge/unmerge changed native LCK state'
        if row['event'] == PREFIX + 'snapshot':
            assert snapshot_matches([row], expected), 'merge/unmerge changed manager snapshot'


def copy_channel(channel, destination, container):
    if not channel.resolve().is_relative_to(container.resolve()):
        raise RuntimeError('Diagnostic channel escaped its owned app container')
    shutil.copytree(channel, destination, dirs_exist_ok=True,
                    ignore=lambda directory, names: [name for name in names if (Path(directory) / name).is_symlink()])


def candidate_containers(applications):
    if not applications.is_dir():
        return
    for container in applications.iterdir():
        try:
            simulator_uuid(container.name)
        except ValueError:
            continue
        if container.is_dir() and not container.is_symlink() and container.resolve().parent == applications.resolve():
            yield container


def container_session(container, run_id, bundle, pid):
    channel = container / 'Documents/.callkit-simulator'
    session_path = channel / 'session.json'
    if not session_path.resolve().is_relative_to(container.resolve()):
        raise RuntimeError('Runtime session path escaped its owned app container')
    try:
        session = json.loads(session_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if session.get('runID') != run_id or session.get('bundleID') != bundle:
        return None
    if type(session.get('pid')) is not int or session['pid'] != pid:
        raise RuntimeError('Fresh runtime session PID does not match simctl launch')
    if session.get('schemaVersion') != 1 or session.get('profile') != 'ios-27.0' or session.get('controlTransport') != 'directory-watch-v1':
        raise RuntimeError('Invalid fresh runtime session protocol')
    journal = channel / run_id / 'events.jsonl'
    if not journal.resolve().is_relative_to(container.resolve()):
        raise RuntimeError('Runtime journal path escaped its owned app container')
    try:
        data = journal.read_bytes()
    except FileNotFoundError:
        return None
    rows = [json.loads(line) for line in data.split(b'\n')[:-1]]
    for index, row in enumerate(rows, 1):
        if row.get('sequence') != index or row.get('schemaVersion') != 1 or any(row.get(k) != session[k] for k in ('runID', 'pid', 'bundleID')):
            raise RuntimeError('Fresh runtime journal identity does not match its session')
    return session if any(r.get('event') == PREFIX + 'ready' and r.get('main') is True for r in rows) else None


def resolve_container(applications, run_id, bundle, pid):
    matches = []
    for container in candidate_containers(applications):
        session = container_session(container, run_id, bundle, pid)
        if session is not None:
            matches.append((container, session))
    if len(matches) > 1:
        raise RuntimeError('Fresh runtime journal matched ambiguous app containers')
    return matches[0] if matches else None


def simulator_applications(device):
    inventory = json.loads(run('xcrun', 'simctl', 'list', 'devices', '-j', timeout=30))['devices']
    matches = [(runtime, row) for runtime, group in inventory.items() for row in group
               if row.get('udid', '').lower() == device.lower()]
    if len(matches) != 1 or matches[0][1].get('state') != 'Booted':
        raise RuntimeError('Expected exactly one owned, booted simulator')
    if matches[0][0] != RUNTIME:
        raise RuntimeError('The borrowed simulator must run iOS 27.0')
    data = Path(matches[0][1]['dataPath'])
    if not data.is_absolute() or data.name != 'data' or data.parent.name.lower() != device.lower():
        raise RuntimeError('Simulator data path does not match its owned UUID')
    return data / 'Containers/Data/Application'


@contextmanager
def simulator_scope(supplied, out):
    ownership = None if supplied else out / 'simulator-id'
    device = simulator_uuid(supplied) if supplied else create(RUNTIME, ownership)
    try:
        yield device, ownership is not None
    finally:
        if ownership is not None:
            cleanup(ownership)


class Journey:
    def __init__(self, device, container, out, *, applications=None):
        self.device, self.container, self.out = device, container, out
        self.applications = applications
        self.channel = container / 'Documents/.callkit-simulator' if container is not None else None
        self.run_id = None
        self.session = None
        self.name = None
        self.ids = {}

    def rows(self):
        assert json.loads((self.channel / 'session.json').read_text()) == self.session, 'runtime session changed'
        data = (self.channel / self.session['runID'] / 'events.jsonl').read_bytes()
        rows = [json.loads(line) for line in data.split(b'\n')[:-1]]
        for index, row in enumerate(rows, 1):
            assert row['sequence'] == index and all(row[k] == self.session[k] for k in ('runID', 'pid', 'bundleID'))
        return rows

    def wait(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.rows()
            result = predicate(rows)
            if result:
                return result
            time.sleep(0.02)
        raise AssertionError(f'{self.name}: timed out waiting for native LCK evidence')

    def begin(self, name, *, groups=2, per_group=1, caps='all'):
        self.save()
        self.name, self.ids, self.session = name, {}, None
        run_id = self.run_id = 'lck-' + str(uuid.uuid4())
        launched = run('xcrun', 'simctl', 'launch', '--terminate-running-process', self.device, BUNDLE,
            env=dict(os.environ, SIMCTL_CHILD_CKSIM_RUN_ID=run_id,
                     SIMCTL_CHILD_LCK_FIXTURE_GROUPS=str(groups), SIMCTL_CHILD_LCK_FIXTURE_PER_GROUP=str(per_group),
                     SIMCTL_CHILD_LCK_FIXTURE_CAPABILITIES=caps), timeout=120)
        launch = re.fullmatch(re.escape(BUNDLE) + r': ([1-9][0-9]*)', launched)
        if launch is None:
            raise RuntimeError('Cannot establish PID from simctl launch output')
        pid = int(launch.group(1))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.container is None:
                found = resolve_container(self.applications, run_id, BUNDLE, pid)
                if found:
                    self.container, self.session = found
                    self.channel = self.container / 'Documents/.callkit-simulator'
            else:
                self.session = container_session(self.container, run_id, BUNDLE, pid)
            if self.session is not None:
                break
            time.sleep(0.02)
        else:
            raise AssertionError('fresh fixture journal never appeared in the owned simulator')
        assert self.session.get('controlTransport') == 'directory-watch-v1', 'spawn-free transport required'
        self.wait(lambda rows: any(r['event'] == PREFIX + 'ready' for r in rows))
        assert len([r for r in self.rows() if r['event'] == 'provider.created']) == 1, 'D13: fixture must create only the LCK provider'
        print('LCK native case: ' + name, flush=True)

    @staticmethod
    def publish(folder, value):
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / (value['id'] + '.tmp')
        with temporary.open('x') as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output)
        temporary.rename(temporary.with_suffix('.json'))

    def app(self, operation, *, expect_success=True, **fields):
        request = dict(id=str(uuid.uuid4()).upper(), operation=operation, runID=self.session['runID'], **fields)
        self.publish(self.container / 'Documents/lck-fixture-requests', request)
        result = self.wait(lambda rows: next((r for r in rows if r['event'] == PREFIX + 'command' and r['requestID'] == request['id']), None))
        assert result['success'] is expect_success, result
        if 'call' in fields:
            self.ids[fields['call']] = result['callUUID']
        return result

    def system(self, operation, *, call='B', active=None, held=None, expect_success=True, **fields):
        request = dict(id=str(uuid.uuid4()).upper(), operation=operation,
                       **{k: self.session[k] for k in ('runID', 'pid', 'bundleID')}, **fields)
        if call:
            request['callUUID'] = self.ids[call]
        if active:
            request['activeCallUUID'] = self.ids[active]
        if held:
            request['heldCallUUID'] = self.ids[held]
        mailbox = self.channel / self.session['runID']
        self.publish(mailbox / 'requests', request)
        response = mailbox / 'responses' / (request['id'] + '.json')
        deadline = time.monotonic() + 10
        while not response.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        result = json.loads(response.read_text())
        assert all(result[k] == request[k] for k in ('id', 'runID', 'pid', 'bundleID'))
        assert result['success'] is expect_success, result
        return result.get('result', {})

    def states(self, rows):
        values = {}
        for row in rows:
            if row['event'] == PREFIX + 'changed':
                values[row['callUUID']] = row['state']
        return {name: values.get(identifier) for name, identifier in self.ids.items()}

    def state(self, **expected):
        return self.wait(lambda rows: rows if all(self.states(rows).get(k) == v for k, v in expected.items()) and
                         snapshot_matches(rows, {self.ids[k]: v for k, v in expected.items()}) else None)

    def pair(self, name, **config):
        self.begin(name, **config)
        self.app('incoming', call='A')
        self.system('answer', call='A')
        self.state(A='joined')
        self.wait(lambda rows: any(r['event'] == PREFIX + 'didActivate' for r in rows))
        self.app('incoming', call='B')

    def choices(self, call):
        # LCK exposes no choice list; compare the modeled policy with #85's UI observations.
        status = self.system('status', call=None)
        current = next(c for c in status['calls'] if c['uuid'] == self.ids[call])
        return set(current['availableSystemChoices'])

    def composite(self, operation='hold-and-accept', *, call='B', active='A', held=None):
        start = len(self.rows())
        result = self.system(operation, call=call, active=active, held=held)
        ids = set(result['actionIDs'])
        rows = self.wait(lambda rows: rows if ids <= {r.get('actionID') for r in rows[start:] if r['event'] == PREFIX + 'perform'} else None)
        native = [r for r in rows[start:] if r['event'] == PREFIX + 'perform' and r.get('actionID') in ids]
        expected = {('Join', self.ids[call]), ('Pause' if operation != 'end-and-accept' else 'End', self.ids[active])}
        if held:
            expected.add(('End', self.ids[held]))
        assert {(r['kind'], r['callUUID']) for r in native} == expected, 'unexpected native composite constituents'
        assert max(r['t_ms'] for r in native) - min(r['t_ms'] for r in native) < 100, 'native composite deliveries separated'
        runtime = [r for r in rows[start:] if r['event'] == 'action.deadline' and r.get('actionID') in ids]
        assert len(runtime) == len(ids) and len({r['transactionID'] for r in runtime}) == 1, 'modeled system envelope must be shared'
        assert all(r['apiKind'] == 'livecommunicationkit' for r in runtime)
        return ids

    def save(self):
        if not self.name:
            return
        out = self.out / self.name
        out.mkdir(parents=True, exist_ok=True)
        if self.channel is not None and self.channel.exists():
            copy_channel(self.channel, out / 'runtime-journal', self.container)
        elif self.applications is not None and self.run_id:
            # Preserve a partial fresh launch even when its PID/checkpoint could
            # not be pinned. This is diagnostics, never acceptance evidence.
            for candidate in candidate_containers(self.applications):
                channel = candidate / 'Documents/.callkit-simulator'
                if not channel.resolve().is_relative_to(candidate.resolve()) or not (channel / 'session.json').resolve().is_relative_to(candidate.resolve()):
                    continue
                try:
                    partial = json.loads((channel / 'session.json').read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    continue
                if partial.get('runID') == self.run_id and partial.get('bundleID') == BUNDLE:
                    copy_channel(channel, out / 'unpinned-runtime-journals' / candidate.name, candidate)
        if self.session is None:
            return
        rows = self.rows()
        (out / 'events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        callbacks = [r for r in rows if r['event'].startswith(PREFIX)]
        assert callbacks and all(r.get('main') is True for r in callbacks), 'LCK callbacks must arrive on main'


def run_cases(journey, *, full_timeout=False):
    j = journey
    j.pair('latePause2s')
    assert j.choices('B') == {'hold-and-accept', 'end-and-accept', 'decline'}
    j.app('policy', rules={'Pause': dict(mode='delay', ms=2000)})
    ids = j.composite()
    rows = j.state(A='paused', B='joined')
    verify_barrier(rows, j.ids['B'], ids)
    pause = next(r for r in rows if r['event'] == PREFIX + 'settled' and r['kind'] == 'Pause')
    delivered_pause = next(r for r in rows if r['event'] == PREFIX + 'perform' and r['actionID'] == pause['actionID'])
    assert 1900 <= pause['t_ms'] - delivered_pause['t_ms'] < 5000, 'late Pause fixture must actually settle about 2s late'
    paused = next(r for r in rows if r['event'] == PREFIX + 'changed' and r.get('callUUID') == j.ids['A'] and r['state'] == 'paused')
    assert paused['t_ms'] < pause['t_ms'], 'pause state must precede delayed fulfillment'
    j.pair('reversedSettlement')
    j.app('policy', rules={kind: dict(mode='reverse', ms=630) for kind in ('Pause', 'Join')})
    ids = j.composite()
    rows = j.state(A='paused', B='joined')
    verify_barrier(rows, j.ids['B'], ids)
    settled = [r['kind'] for r in rows if r['event'] == PREFIX + 'settled' and r.get('actionID') in ids]
    assert settled == ['Join', 'Pause'], settled
    for name, operation, failing, expected in (
        ('failedPause', 'hold-and-accept', 'Pause', dict(A='left', B='joined')),
        ('failedJoinHeld', 'hold-and-accept', 'Join', dict(A='paused', B='left')),
        ('failedEnd', 'end-and-accept', 'End', dict(A='left', B='joined')),
        ('failedJoinEnded', 'end-and-accept', 'Join', dict(A='left', B='left'))):
        j.pair(name)
        j.app('policy', rules={failing: dict(mode='fail', nth=1)})
        ids = j.composite(operation)
        rows = j.state(**expected)
        if failing in ('Pause', 'Join'):
            target = j.ids['A' if failing == 'Pause' else 'B']
            j.wait(lambda rows: any(r['event'] == PREFIX + 'perform' and r['kind'] == 'End' and
                                   r['callUUID'] == target and r['actionID'] not in ids for r in rows))
        if expected.get('B') == 'joined':
            verify_barrier(rows, j.ids['B'], ids)
        if expected.get('A') == 'paused':
            time.sleep(0.3)
            assert j.states(j.rows())['A'] == 'paused', 'failed Join must not resume held A'
    timeouts = [('pendingPause', 'hold-and-accept', 'Pause', dict(A='left', B='joined')),
                ('pendingEnd', 'end-and-accept', 'End', dict(A='left', B='joined'))]
    if full_timeout:
        timeouts.append(('pendingJoin64s', 'hold-and-accept', 'Join', dict(A='paused', B='left')))
    for name, operation, kind, expected in timeouts:
        j.pair(name)
        j.app('policy', rules={kind: dict(mode='pending', nth=1)})
        ids = j.composite(operation)
        delivered = j.wait(lambda rows: next((r for r in rows if r['event'] == PREFIX + 'perform' and r.get('actionID') in ids and r['kind'] == kind), None))
        expired = j.wait(lambda rows: next((r for r in rows if r['event'] == PREFIX + 'timedOut' and r.get('actionID') == delivered['actionID']), None), timeout=70 if kind == 'Join' else 12)
        verify_timeout(delivered, expired, kind)
        rows = j.state(**expected)
        if kind in ('Pause', 'Join'):
            target = j.ids['A' if kind == 'Pause' else 'B']
            j.wait(lambda rows: any(r['event'] == PREFIX + 'perform' and r['kind'] == 'End' and
                                   r['callUUID'] == target and r['actionID'] not in ids for r in rows))
        if expected.get('B') == 'joined':
            verify_barrier(rows, j.ids['B'], ids)
    j.pair('capabilitiesNone', caps='none')
    assert 'hold-and-accept' in j.choices('B')
    j.composite()
    j.state(A='paused', B='joined')
    j.app('pause', call='B', paused=True)
    j.state(A='paused', B='paused')
    j.pair('capsToggle')
    j.app('caps', call='A', capabilities=[])
    assert 'hold-and-accept' in j.choices('B')
    j.composite()
    j.state(A='paused', B='joined')
    j.app('caps', call='B', capabilities=[])
    j.app('pause', call='B', paused=True)
    j.state(A='paused', B='paused')
    j.pair('mergeUnmerge', per_group=2)
    j.composite()
    j.state(A='paused', B='joined')
    for operation, fields in (('merge', dict(call='B', other='A')), ('merge', dict(call='A', other='B')),
                              ('unmerge', dict(call='A')), ('unmerge', dict(call='B'))):
        start = len(j.rows())
        j.app(operation, **fields)
        j.wait(lambda rows: any(r['event'] == PREFIX + 'settled' and r['kind'] == operation.capitalize() for r in rows[start:]))
        time.sleep(0.2)
        j.app('snapshot')
        verify_unchanged(j.rows()[start:], {j.ids['A']: 'paused', j.ids['B']: 'joined'})
    j.app('incoming', call='C')
    assert j.choices('C') == {'end-and-accept', 'end-held-and-accept', 'decline'}
    result = j.app('start', call='D', expect_success=False)
    assert (result['errorDomain'], result['errorCode']) == ('com.apple.CallKit.error.requesttransaction', 7)
    ids = j.composite('end-held-and-accept', call='C', active='B', held='A')
    j.state(A='left', B='paused', C='joined')
    j.pair('thirdEndActive')
    j.composite()
    j.state(A='paused', B='joined')
    j.app('incoming', call='C')
    j.composite('end-and-accept', call='C', active='B')
    j.state(A='paused', B='left', C='joined')
    j.begin('unmergeSingleton', per_group=2)
    j.app('incoming', call='A')
    j.system('answer', call='A')
    j.state(A='joined')
    start = len(j.rows())
    j.app('unmerge', call='A')
    j.wait(lambda rows: any(r['event'] == PREFIX + 'settled' and r['kind'] == 'Unmerge' for r in rows[start:]))
    time.sleep(0.2)
    j.app('snapshot')
    verify_unchanged(j.rows()[start:], {j.ids['A']: 'joined'})
    j.pair('resumeEndsActive')
    j.composite()
    j.state(A='paused', B='joined')
    start = len(j.rows())
    j.app('pause', call='A', paused=False)
    rows = j.state(A='joined', B='left')
    observed = [r for r in rows[start:] if r['event'] == PREFIX + 'perform' and r['callUUID'] == j.ids['B']]
    assert [r['kind'] for r in observed] == ['End'], 'resume must end active B, not pause it'
    for per_group in (1, 2):
        j.pair('oneGroup' + str(per_group), groups=1, per_group=per_group)
        assert j.choices('B') == {'end-and-accept', 'decline'}
        result = j.app('start', call='C', expect_success=False)
        assert (result['errorDomain'], result['errorCode']) == ('com.apple.CallKit.error.requesttransaction', 7)
    j.save()


def run_filter_case(j):
    j.begin('explicitFilter')
    j.app('incoming', call='normal', handle='+15555550199')
    j.system('answer', call='normal')
    j.state(normal='joined')
    j.system('end', call='normal')
    j.state(normal='left')
    j.system('faults', call=None, faults=dict(staleAudio=False, reportCallbackDelayMS=0,
             invalidateDuringHandoff=False, incomingErrorCode=4))
    filtered = j.app('incoming', call='filtered', handle='+15555550199', expect_success=False)
    assert (filtered['errorDomain'], filtered['errorCode']) == ('com.apple.CallKit.error.incomingcall', 4)
    j.system('faults', call=None, faults=dict(staleAudio=False, reportCallbackDelayMS=0,
             invalidateDuringHandoff=False, incomingErrorCode=0))
    j.app('incoming', call='cleared', handle='+15555550199')
    j.system('answer', call='cleared')
    j.state(cleared='joined')
    j.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--simulator', type=simulator_uuid, help='Borrow this already booted operator-owned simulator; never shut it down or delete it')
    parser.add_argument('--full-timeout', action='store_true', help='Also observe one full ~64s pending Join')
    parser.add_argument('--all', action='store_true', help='Run filters, full-timeout multicall, focused handoff, and Inspector app controls after one install')
    parser.add_argument('--handoff-only', action='store_true', help='Observe focused Join/Start/Mute/composite/resume audio wiring')
    parser.add_argument('--filter-only', action='store_true', help='Normal report → explicit incoming code4 → cleared normal report')
    args = parser.parse_args()
    if args.all and (args.filter_only or args.handoff_only) or args.handoff_only and args.filter_only or args.full_timeout and (args.handoff_only or args.filter_only):
        parser.error('Choose a single acceptance mode; --full-timeout belongs to multicall acceptance')
    args.out.mkdir(parents=True, exist_ok=True)
    runtimes = json.loads(run('xcrun', 'simctl', 'list', 'runtimes', '-j', timeout=30))['runtimes']
    assert any(r['identifier'] == RUNTIME and r.get('isAvailable') for r in runtimes), 'iOS 27.0 runtime required'
    with simulator_scope(args.simulator, args.out) as (device, owned):
        journey = None
        try:
            if owned:
                run('xcrun', 'simctl', 'boot', device, timeout=60)
                run('xcrun', 'simctl', 'bootstatus', device, '-b', timeout=300)
            applications = simulator_applications(device)
            run('xcrun', 'simctl', 'install', device, args.app.resolve(), timeout=120)
            journey = Journey(device, None, args.out, applications=applications)
            if args.all:
                from lck_handoff_cases import run_cases as run_handoff_cases
                run_filter_case(journey)
                run_cases(journey, full_timeout=True)
                run_handoff_cases(journey)
                inspector_controls = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'Inspector/lck_app_controls.py'))
                inspector_controls['run_case'](journey, args.out / 'inspector-model-controls')
            elif args.filter_only:
                run_filter_case(journey)
            elif args.handoff_only:
                from lck_handoff_cases import run_cases as run_handoff_cases
                run_handoff_cases(journey)
            else:
                run_cases(journey, full_timeout=args.full_timeout)
            print(json.dumps(dict(success=True, fixture='genuine ConversationManager', runtime=RUNTIME,
                                  mode='all' if args.all else 'explicit-incoming-fault' if args.filter_only else 'focused-handoff' if args.handoff_only else 'measured-multicall',
                                  fullJoinTimeout=args.all or args.full_timeout, choicesAndTransactionIDs='modeled policy assertions')))
        finally:
            failed = sys.exc_info()[0] is not None
            if journey is not None:
                try:
                    journey.save()
                except Exception as error:
                    if not failed:
                        raise
                    print('Diagnostic capture failed: ' + str(error), file=sys.stderr)


if __name__ == '__main__':
    main()
