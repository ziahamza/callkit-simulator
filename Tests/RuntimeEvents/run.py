#!/usr/bin/env python3
"""Native acceptance against a disposable simulator, preserving only ignored/CI output."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = 'dev.callkitsim.eventfixture'


def latest_runtime(runtimes, required=None):
    available = [r for r in runtimes if r.get('isAvailable') and r.get('platform') == 'iOS']
    if required:
        available = [r for r in available if r['identifier'] == required]
    if not available:
        raise RuntimeError('No available iOS simulator runtime' + (': ' + required if required else ''))
    return max(available, key=lambda r: tuple(int(part) for part in r['version'].split('.')))['identifier']


def run(*args, timeout=120, **kwargs):
    try:
        result = subprocess.run(list(map(str, args)), text=True, capture_output=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f'{args[0]} timed out after {timeout}s: {error.stdout!r}\n{error.stderr!r}') from error
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stdout}\n{result.stderr}')
    return result.stdout.strip()


def remove_simulators(devices):
    errors = []
    for device in reversed(devices):
        for operation in ('shutdown', 'delete'):
            try:
                run('xcrun', 'simctl', operation, device, timeout=30)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                errors.append(str(error))
    if errors:
        if sys.exc_info()[0] is None:
            raise RuntimeError('\n'.join(errors))
        print('\n'.join(errors), file=sys.stderr)


def wait(predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError('Native event acceptance timed out')


def record_environment(out, runtime):
    out.mkdir(parents=True, exist_ok=True)
    (out / 'environment.txt').write_text(run('xcodebuild', '-version') + '\n' + runtime + '\n')
    (out / 'runtimes.json').write_text(run('xcrun', 'simctl', 'list', 'runtimes', '-j') + '\n')


def launch_fixture(device, app, *, install=True):
    """Start a fresh session; the host sends the first stimulus after readiness."""
    run_id = 'fixture-' + str(uuid.uuid4())
    if install:
        run('xcrun', 'simctl', 'install', device, app.resolve())
    run('xcrun', 'simctl', 'launch', '--terminate-running-process', device, BUNDLE,
        env=dict(os.environ, SIMCTL_CHILD_CKSIM_RUN_ID=run_id))
    container = Path(run('xcrun', 'simctl', 'get_app_container', device, BUNDLE, 'data'))
    channel = container / 'Documents/.callkit-simulator'
    return channel, run_id


def fixture_session(channel, run_id):
    """Wait separately so the caller retains the channel if startup fails."""
    def ready():
        try:
            session = json.loads((channel / 'session.json').read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return session if session.get('runID') == run_id else None
    return wait(ready)


def capture_diagnostics(out, device, *, bundle=BUNDLE, channel=None, failed=False):
    """Copy the original channel before deleting its simulator, even on failure."""
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print('Cannot create diagnostics directory: ' + str(error), file=sys.stderr)
        return
    errors = []
    def attempt(operation):
        try:
            operation()
        except Exception as error:
            errors.append(str(error))
    def journal():
        source = channel
        if source is None:
            container = Path(run('xcrun', 'simctl', 'get_app_container', device, bundle, 'data', timeout=20))
            source = container / 'Documents/.callkit-simulator'
        shutil.copytree(source, out / 'runtime-journal', dirs_exist_ok=True)
    attempt(journal)
    if failed:
        attempt(lambda: run('xcrun', 'simctl', 'io', device, 'screenshot', out / 'failure.png', timeout=20))
        def logs():
            output = run('xcrun', 'simctl', 'spawn', device, 'log', 'show', '--last', '5m',
                         '--style', 'compact', '--predicate',
                         'process == "RuntimeEventFixture" OR process == "CKSimInspector"', timeout=20)
            (out / 'simulator.log').write_text(output + '\n')
        attempt(logs)
    if errors:
        try:
            (out / 'diagnostic-errors.txt').write_text('\n'.join(errors) + '\n')
        except OSError as error:
            print('Cannot save diagnostic errors: ' + str(error), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--simulator')
    parser.add_argument('--runtime', help='Require this exact available simulator runtime identifier')
    args = parser.parse_args()
    args.out.mkdir(parents=True)
    runtime = latest_runtime(json.loads(run('xcrun', 'simctl', 'list', 'runtimes', '-j'))['runtimes'], args.runtime)
    record_environment(args.out, runtime)
    print('Selected simulator runtime: ' + runtime, flush=True)
    device = args.simulator or run('xcrun', 'simctl', 'create', 'Codex M4 ' + str(uuid.uuid4())[:8],
                                   'com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro', runtime)
    def sim(*parts, **kwargs):
        return run('xcrun', 'simctl', *parts, **kwargs)
    def command(*parts):
        result = json.loads(run(ROOT / 'Tools/cksim', '--simulator', device, '--bundle', BUNDLE,
                                '--runtime-dir', args.package.resolve(), *parts))
        assert result.get('success'), result
        return result
    root = None
    try:
        # A supplied simulator must also belong to this test run's operator.
        devices = json.loads(sim('list', 'devices', '-j'))['devices']
        state = next(d['state'] for group in devices.values() for d in group if d['udid'] == device)
        if state != 'Booted':
            sim('boot', device)
        sim('bootstatus', device, '-b', timeout=300)
        root, run_id = launch_fixture(device, args.app)
        session = fixture_session(root, run_id)
        assert session.get('controlTransport') == 'directory-watch-v1', 'fixture must support spawn-free mailbox controls'
        journal = root / session['runID'] / 'events.jsonl'
        def rows():
            return [json.loads(line) for line in journal.read_text().splitlines()]
        def calls():
            projected = {}
            for row in rows():
                if row['event'] == 'call.changed':
                    projected[row['uuid']] = row
            return [r for r in projected.values() if not r['ended']]
        def state(call, **values):
            return wait(lambda: next((c for c in calls() if c['uuid'] == call and all(c[k] == v for k,v in values.items())), None))
        # A launch timer races slow simctl transport against the ring timeout.
        # Inject only after the host can observe this exact runtime session.
        payload = args.out / 'push.json'
        payload.write_text(json.dumps(dict(operation='incoming')))
        command('push', '--payload-file', payload, '--must-report', 'false')
        first = wait(lambda: calls())[0]['uuid']
        command('answer', first)
        state(first, connected=True)
        wait(lambda: any(r['event'] == 'audio.activated' for r in rows()))
        command('mute', first, '--muted', 'true')
        state(first, muted=True)
        command('tones', first, '--digits', '12#')
        wait(lambda: any(r['event'] == 'action.settled' and not r['fulfilled'] for r in rows()))
        for operation in ('decline', 'hold-and-accept', 'end-and-accept'):
            previous = {c['uuid'] for c in calls()}
            payload.write_text(json.dumps(dict(operation='incoming')))
            command('push', '--payload-file', payload, '--must-report', 'false')
            incoming = wait(lambda: next((c['uuid'] for c in calls() if c['uuid'] not in previous), None))
            command(operation, incoming, *([] if operation == 'decline' else ['--active-call', first]))
            if operation != 'decline':
                state(incoming, connected=True)
                if operation == 'hold-and-accept':
                    state(first, held=True)
                first = incoming
            else:
                wait(lambda: all(c['uuid'] != incoming for c in calls()))
        for call in list(calls()):
            command('end', call['uuid'])
        wait(lambda: not calls())
        payload.write_text(json.dumps(dict(operation='outgoing')))
        command('push', '--payload-file', payload, '--must-report', 'false')
        wait(lambda: any(r['event'] == 'report.connected' for r in rows()))
        for call in list(calls()):
            command('end', call['uuid'])
        wait(lambda: not calls())
        captured = run(ROOT / 'Tools/cksim', '--simulator', device, '--bundle', BUNDLE,
                       'watch', '--json', '--duration', '0.3')
        watched = [json.loads(line) for line in captured.splitlines()]
        assert watched and watched == rows()[:len(watched)], 'watch differs from runtime journal'
        for index, row in enumerate(watched, 1):
            assert row['schemaVersion'] == 1 and row['sequence'] == index
            assert all(row[k] == session[k] for k in ('runID', 'bundleID', 'pid'))
            assert isinstance(row['wallTimeMS'], int)
            if row['event'].startswith('action.'):
                assert all(k in row for k in ('kind','origin','callUUID','transactionID','apiKind','deadlineMS','deadlineSeconds','latencyMS'))
                assert row['latencyMS'] >= 0
            if row['event'] == 'action.settled':
                assert 50 <= row['latencyMS'] < row['deadlineSeconds'] * 1000
        events = {r['event'] for r in watched}
        assert {'report.incoming','report.completed','report.updated','report.connecting','report.connected',
                'action.deadline','action.settled','call.changed','audio.activated','push.received','push.completed'} <= events
        (args.out / 'events.jsonl').write_text(captured + '\n')
        (args.out / 'environment.txt').write_text(run('xcodebuild', '-version') + '\n' + runtime + '\n')
        print(json.dumps(dict(success=True, events=len(watched), channelMatchesWatch=True)))
    finally:
        capture_diagnostics(args.out, device, channel=root, failed=sys.exc_info()[0] is not None)
        if not args.simulator:
            remove_simulators([device])


if __name__ == '__main__':
    main()
