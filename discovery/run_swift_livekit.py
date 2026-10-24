#!/usr/bin/env python3
"""Two real Swift engine modes on exactly one new owned simulator, without injection."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

from build_swift_livekit_contract import ROOT, digest, save, source
from summarize import one, require, selected_runtime
from swift_livekit_conformance import BUNDLE, CASES, CLASSIFICATION, validate, validate_build, published_container

SCENARIO_TIMEOUT_SECONDS = 90


def command(out, label, arguments, timeout=60, check=True, env=None):
    try:
        result = subprocess.run([str(value) for value in arguments], capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as error:
        (out / (label + '.stdout')).write_bytes(b'' if label == 'launch-environment' else error.stdout or b'')
        (out / (label + '.stderr')).write_bytes(error.stderr or b'')
        save(out / (label + '.status.json'), dict(exitCode=None, error=str(error)))
        raise
    # The current launchd value is passed to app launches only in memory.
    (out / (label + '.stdout')).write_bytes(b'' if label == 'launch-environment' else result.stdout)
    (out / (label + '.stderr')).write_bytes(result.stderr)
    save(out / (label + '.status.json'), dict(exitCode=result.returncode))
    require(not check or result.returncode == 0, label + ' failed: ' + str(result.returncode))
    return result


def live(path):
    if not path.exists(): return []
    return [json.loads(line.split(b'PROBE ', 1)[1]) for line in path.read_bytes().splitlines(keepends=True)
            if line.endswith(b'\n') and b'PROBE ' in line]


def wait(process, condition, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value: return value
        require(process.poll() is None, 'supervisor exited before fixture checkpoint')
        time.sleep(.025)
    raise RuntimeError('fixture checkpoint deadline')


def fault(out, build, simulator, runtime, session):
    # The existing mailbox protocol, with the source-built notifier. No CLI
    # container query or alternate runtime is needed for this test-only transport.
    request = {key: session[key] for key in ('runID', 'pid', 'bundleID')}
    request.update(id=str(uuid.uuid4()).upper(), operation='faults',
                   faults=dict(staleAudio=True, reportCallbackDelayMS=0, invalidateDuringHandoff=False))
    save(out / 'fault-request.json', request)
    temporary = runtime / 'requests' / (request['id'] + '.tmp')
    with temporary.open('x') as stream:
        os.chmod(temporary, 0o600); json.dump(request, stream)
    temporary.rename(temporary.with_suffix('.json'))
    command(out, 'fault-notify', ['xcrun', 'simctl', 'spawn', simulator, build / 'cksim-notify', session['notification']])
    return runtime / 'responses' / (request['id'] + '.json')


def capture_case(build, out, simulator, inherited, mode, expected_container=None):
    run_id = str(uuid.uuid4()); trace, receipt = out / (mode + '.log'), out / (mode + '.receipt.json')
    runtime = None
    env = {key: value for key, value in os.environ.items() if not key.startswith('SIMCTL_CHILD_')}
    env.update(SIMCTL_CHILD_DYLD_INSERT_LIBRARIES=inherited, SIMCTL_CHILD_CKSIM_RUN_ID=run_id,
               SIMCTL_CHILD_SDK_SWIFT_LIVEKIT_CASE=mode)
    argv = [sys.executable, ROOT / 'discovery/run_scenario.py', '--console-launcher-stop', '--inherit-launcher-group', '--timeout', str(SCENARIO_TIMEOUT_SECONDS),
            '--output', trace, '--receipt', receipt, '--run-id', run_id, '--simulator-id', simulator,
            '--scenario', mode, '--bundle-id', BUNDLE, '--terminate-command', 'xcrun', 'simctl', 'terminate', simulator, BUNDLE,
            '--launch-command', 'xcrun', 'simctl', 'launch', '--console', simulator, BUNDLE]
    with (out / (mode + '.supervisor.log')).open('xb') as log:
        process = subprocess.Popen([str(value) for value in argv], env=env, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        cleanup_required = False
        try:
            launch = wait(process, lambda: next((row for row in live(trace) if row['event'] == 'launch'), None), SCENARIO_TIMEOUT_SECONDS)
            entry = one(live(trace), 'app.entry')
            require(all(entry.get(key) == launch.get(key) for key in ('runID', 'pid', 'bundleID', 'simulatorID')),
                    'fresh entry/launch identity differs')
            container = published_container(launch, simulator, run_id, entry.get('pid'), mode,
                                            digest(build / 'SDKSwiftLiveKit.app/SDKSwiftLiveKit'))
            require(expected_container is None or str(container) == expected_container,
                    'fresh launch changed installation container')
            try:
                require(container.resolve(strict=True) == container and container.is_dir(),
                        'published container is missing or symlinked')
            except OSError:
                require(False, 'published container is missing or symlinked')
            root = container / 'Documents/.callkit-simulator'
            session = wait(process, lambda: json.loads((root / 'session.json').read_text())
                if (root / 'session.json').exists() and json.loads((root / 'session.json').read_text()).get('runID') == run_id else None, 15)
            require(session.get('runID') == run_id and session.get('pid') == launch['pid'] and session.get('bundleID') == BUNDLE and
                    session.get('notification') == 'dev.callkitsim.control.' + str(launch['pid']) and
                    session.get('profile') == 'ios-27.0', 'fresh runtime session differs')
            runtime = root / run_id
            save(out / (mode + '.session.json'), session)
            if mode == 'stale-audio':
                wait(process, lambda: any(row['event'] == 'livekit.readyForFault' for row in live(trace)), 15)
                response = fault(out, build, simulator, runtime, session)
                wait(process, response.exists, 10)
                shutil.copyfile(response, out / 'fault-response.json')
            require(process.wait(timeout=100) == 0, 'supervisor refused native capture')
        finally:
            handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                if process.poll() is None:
                    cleanup_required = True
                    diagnostic = dict(supervisorPID=process.pid, forcedGroupKill=False)
                    try:
                        command(out, mode + '-failed-stop', ['xcrun', 'simctl', 'terminate', simulator, BUNDLE],
                                timeout=10, check=False)
                    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                        diagnostic['appStopError'] = str(error)
                    try:
                        process.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        # The opt-in helper verifies its launcher inherits this
                        # private group. Never signal a group after owner reap.
                        try:
                            if process.poll() is None:
                                require(os.getpgid(process.pid) == process.pid == os.getsid(process.pid),
                                        'supervisor private process-group ownership is unknown')
                                try:
                                    os.killpg(process.pid, signal.SIGKILL)
                                    diagnostic['forcedGroupKill'] = True
                                except ProcessLookupError:
                                    pass
                        finally:
                            process.wait(timeout=5)
                    save(out / (mode + '-failed-cleanup.json'), diagnostic)
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    group_absent = True
                else:
                    group_absent = False
                require(group_absent, 'supervisor/console group remains or cleanup is unknown')
                if receipt.exists():
                    value = json.loads(receipt.read_text()); value['supervisorGroupAbsentAfterStop'] = True
                    save(receipt, value)
                journal = runtime / 'events.jsonl' if runtime is not None else None
                if journal is not None and journal.exists():
                    shutil.copyfile(journal, out / (mode + '.runtime.jsonl'))
                    if receipt.exists():
                        value = json.loads(receipt.read_text()); value['journalSHA256'] = digest(out / (mode + '.runtime.jsonl'))
                        save(receipt, value)
            finally:
                for sig, handler in handlers.items(): signal.signal(sig, handler)
        require(not cleanup_required, 'failure cleanup cannot qualify a native journey')
    return dict(runID=run_id, pid=launch['pid'], supervisorPID=process.pid, dataContainer=str(container))



def redact(out, inherited):
    for path in out.iterdir():
        if path.suffix not in ('.log', '.stdout', '.stderr'): continue
        raw = data = path.read_bytes()
        for value in filter(None, inherited.split(':')): data = data.replace(value.encode(), b'<inherited-external-library>')
        if data == raw: continue
        path.write_bytes(data)
        receipt = path.with_suffix('.receipt.json')
        if receipt.exists():
            row = json.loads(receipt.read_text())
            row.update(unredactedTraceSHA256=row['traceSHA256'], traceSHA256=digest(path), externalLibraryPathsRedacted=True)
            save(receipt, row)


def capture(build, out):
    validate_build(build); out.mkdir()
    simulator = None; inherited = ''; name = 'sdk-swift-livekit-' + str(uuid.uuid4())
    cases = {}
    # Match M0 ownership/EXIT semantics: create once, recover only our exact name
    # if creation times out, delete only that simulator, then independently list.
    try:
        runtimes = json.loads(command(out, 'runtimes', ['xcrun', 'simctl', 'list', 'runtimes', '-j']).stdout)
        runtime = selected_runtime(runtimes, os.environ.get('SDK_SWIFT_LIVEKIT_RUNTIME'))
        require(int(runtime['version'].split('.')[0]) >= 27, 'an iOS 27+ simulator runtime is required')
        save(out / 'runtime.json', {key: runtime[key] for key in ('identifier', 'version', 'buildversion')})
        created = command(out, 'create', ['xcrun', 'simctl', 'create', name,
            'com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro', runtime['identifier']], 30)
        simulator = str(uuid.UUID(created.stdout.decode().strip())).upper()
        command(out, 'boot', ['xcrun', 'simctl', 'boot', simulator])
        command(out, 'bootstatus', ['xcrun', 'simctl', 'bootstatus', simulator, '-b'], 300)
        configured = os.environ.get('SIMCTL_CHILD_DYLD_INSERT_LIBRARIES')
        if configured is None:
            # Match M0's cold-start infrastructure budget. The control process
            # must not load a stale external library while reading launchd.
            result = command(out, 'launch-environment', ['xcrun', 'simctl', 'spawn', simulator, 'launchctl',
                'getenv', 'DYLD_INSERT_LIBRARIES'], timeout=180, check=False,
                env={**os.environ, 'SIMCTL_CHILD_DYLD_INSERT_LIBRARIES': ''})
            require(result.returncode in (0, 1) and not result.stderr and
                    (result.returncode == 0 or not result.stdout), 'cannot read inherited launch environment')
            inherited = result.stdout.decode().removesuffix('\n')
        else:
            inherited = configured
        require(not any(Path(value).name in ('CKSimRuntime.dylib', 'CKSimLoader.dylib') for value in inherited.split(':')),
                'external SDK runtime injection refused')
        require(not any(key.startswith('SIMCTL_CHILD_CKSIM_') and value for key, value in os.environ.items()),
                'inherited runtime child override refused')
        save(out / 'inherited-dyld.json', dict(present=bool(inherited), sha256=hashlib.sha256(inherited.encode()).hexdigest()))
        command(out, 'install', ['xcrun', 'simctl', 'install', simulator, build / 'SDKSwiftLiveKit.app'], 180)
        command(out, 'microphone', ['xcrun', 'simctl', 'privacy', simulator, 'grant', 'microphone', BUNDLE])
        container = None
        for mode in CASES:
            cases[mode] = capture_case(build, out, simulator, inherited, mode, container)
            container = cases[mode]['dataContainer']
    finally:
        handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            if simulator is None:
                # A timed-out create can still have registered the owned name.
                rows = json.loads(command(out, 'creation-recovery', ['xcrun', 'simctl', 'list', 'devices', '-j']).stdout)
                matches = [row['udid'] for values in rows['devices'].values() for row in values if row.get('name') == name]
                require(len(matches) <= 1, 'ambiguous owned simulator creation; cleanup unknown')
                if matches: simulator = str(uuid.UUID(matches[0])).upper()
            if simulator:
                codes = {}
                for operation in ('shutdown', 'delete'):
                    try: codes[operation] = command(out, operation, ['xcrun', 'simctl', operation, simulator], 60, check=False).returncode
                    except (OSError, subprocess.TimeoutExpired) as error:
                        codes[operation] = None
                        save(out / (operation + '.status.json'), dict(exitCode=None, error=str(error)))
                save(out / 'cleanup.json', dict(simulatorID=simulator, shutdownExitCode=codes['shutdown'],
                    deleteExitCode=codes['delete'], deleted=codes['delete'] == 0))
                remaining = command(out, 'devices-after', ['xcrun', 'simctl', 'list', 'devices', '-j'])
                (out / 'devices-after.json').write_bytes(remaining.stdout)
                require(codes['delete'] == 0 and not any(row.get('udid') == simulator for values in
                    json.loads(remaining.stdout)['devices'].values() for row in values), 'owned simulator cleanup/absence failed')
        finally:
            try: redact(out, inherited)
            finally:
                for sig, handler in handlers.items(): signal.signal(sig, handler)
    require(set(cases) == set(CASES), 'missing native mode')
    save(out / 'manifest.json', dict(schemaVersion=1, source=source(), simulatorID=simulator, cases=cases,
        classification=CLASSIFICATION, ownsSimulator=True, runtimeInjectionRequested=False,
        buildInfoSHA256=digest(build / 'build-info.json'),
        files={str(path.relative_to(out)): digest(path) for path in out.rglob('*') if path.is_file()}))
    result = validate(build, out); save(out / 'summary.json', result); print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path); parser.add_argument('out', type=Path)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    capture(args.build.resolve(), args.out.resolve())
