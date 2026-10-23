#!/usr/bin/env python3
"""Run two pinned Expo local-demand/engine cases on one throwaway simulator."""
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

from expo_livekit_conformance import product_files, validate, validate_build
from sdk_m0_conformance import ROOT, source_identity

BUNDLE = 'dev.iosvoipsdk.example'


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def handle_termination(signum, _frame):
    # Convert CI cancellation into an unwind through owned-process/sim cleanup.
    signal.signal(signum, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def require_group_absent(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return
    raise RuntimeError('owned supervisor group remains; cleanup is unknown')


def sim(args, out, timeout=30, check=True, env=None):
    result = subprocess.run(['xcrun', 'simctl', *map(str, args)], capture_output=True,
                            timeout=timeout, env=env)
    with (out / 'simctl.log').open('ab') as stream:
        # Never copy a launchd environment value into retained logs.
        stream.write(result.stderr)
    if check and result.returncode:
        raise RuntimeError('simctl ' + str(args[0]) + ' failed; inspect simctl.log')
    return result


def wait_for(read, process, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = read()
        if result is not None:
            return result
        if process.poll() is not None:
            raise RuntimeError('supervisor exited before runtime control readiness')
        time.sleep(.05)
    raise RuntimeError('runtime control readiness deadline')


def configure(out, data, name, case, run_id, udid, process, notifier):
    path = data / 'Documents/.callkit-simulator/session.json'

    def session():
        if path.is_symlink():
            raise RuntimeError('runtime session symlink refused')
        if not path.exists():
            return None
        raw = path.read_bytes()
        value = json.loads(raw)
        pid = value.get('pid')
        expected = dict(schemaVersion=1, runID=run_id, pid=pid, bundleID=BUNDLE,
                        profile='ios-27.0', notification=f'dev.callkitsim.control.{pid}')
        if type(pid) is not int or pid <= 0 or type(value.get('schemaVersion')) is not int or value != expected:
            raise RuntimeError('runtime session identity differs')
        return raw, value

    raw, value = wait_for(session, process, 30)
    (out / (name + '.session.json')).write_bytes(raw)
    nonce = str(uuid.uuid4()).upper()
    request = dict(id=nonce, runID=run_id, pid=value['pid'], bundleID=BUNDLE, operation='faults',
                   faults=dict(staleAudio=case == 'stale-audio', reportCallbackDelayMS=0, invalidateDuringHandoff=False))
    write(out / (name + '.request.json'), request)
    control = data / 'Documents/.callkit-simulator' / run_id
    pending = control / 'requests' / (nonce + '.tmp')
    write(pending, request)
    pending.rename(pending.with_suffix('.json'))
    sim(['spawn', udid, notifier, value['notification']], out, timeout=10)
    response_path = control / 'responses' / (nonce + '.json')
    wait_for(lambda: response_path if response_path.is_file() else None, process, 5)
    response = json.loads(response_path.read_text())
    if response != dict(id=nonce, runID=run_id, pid=value['pid'], bundleID=BUNDLE,
                        success=True, result=dict(configured=True)):
        raise RuntimeError('runtime fault request was not acknowledged')
    shutil.copyfile(response_path, out / (name + '.response.json'))
    ready = dict(runID=run_id, case=case, pid=value['pid'], staleAudio=case == 'stale-audio')
    write(out / (name + '.ready.json'), ready)
    pending = data / 'tmp' / ('sdk-m2-control-' + run_id + '.tmp')
    write(pending, ready)
    pending.rename(pending.with_suffix('.json'))
    return raw


def scenario(build, out, data, case, run_id, udid, inherited, previous_session):
    name = 'expo-livekit-' + case
    session = data / 'Documents/.callkit-simulator/session.json'
    if session.exists() or session.is_symlink():
        if session.is_symlink() or previous_session is None or session.read_bytes() != previous_session:
            raise RuntimeError('refuse to retire an unknown runtime session')
        session.unlink()
    env = os.environ.copy()
    env.update(SIMCTL_CHILD_DYLD_INSERT_LIBRARIES=inherited, SIMCTL_CHILD_CKSIM_RUN_ID=run_id,
               SIMCTL_CHILD_SDK_M2_RUN_ID=run_id, SIMCTL_CHILD_SDK_M2_CASE=case)
    args = [sys.executable, ROOT / 'discovery/run_scenario.py', '--console-launcher-stop', '--inherit-launcher-group',
            '--timeout', '120',
            '--output', out / (name + '.log'), '--receipt', out / (name + '.receipt.json'),
            '--run-id', run_id, '--simulator-id', udid, '--scenario', name, '--bundle-id', BUNDLE,
            '--terminate-command', 'xcrun', 'simctl', 'terminate', udid, BUNDLE,
            '--launch-command', 'xcrun', 'simctl', 'launch', '--console', '--terminate-running-process', udid, BUNDLE]
    raw = None
    with (out / (name + '.supervisor.log')).open('xb') as log:
        process = subprocess.Popen(list(map(str, args)), env=env, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            raw = configure(out, data, name, case, run_id, udid, process, build / 'cksim-notify')
            if process.wait(timeout=160):
                raise RuntimeError(name + ' failed; inspect supervisor log')
        finally:
            handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                if process.poll() is None:
                    try:
                        sim(['terminate', udid, BUNDLE], out, timeout=10, check=False)
                    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                        with (out / (name + '.supervisor.log')).open('a') as diagnostic:
                            diagnostic.write(str(error) + '\n')
                    # Let the owned app stop unwind the supervisor naturally. If
                    # its deadline expires, this private session contains only our
                    # supervisor and console children; never signal a shared group.
                    try:
                        process.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        if process.poll() is None:
                            if os.getpgid(process.pid) != process.pid or os.getsid(process.pid) != process.pid:
                                raise RuntimeError('supervisor no longer owns its private group; cleanup is unknown')
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        process.wait(timeout=5)
                require_group_absent(process.pid)
                journal = data / 'Documents/.callkit-simulator' / run_id / 'events.jsonl'
                if journal.is_file():
                    shutil.copyfile(journal, out / (name + '.runtime.jsonl'))
                    receipt = out / (name + '.receipt.json')
                    if receipt.is_file():
                        value = json.loads(receipt.read_text())
                        value['journalSHA256'] = hashlib.sha256(journal.read_bytes()).hexdigest()
                        write(receipt, value)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
    return raw, process.pid


def cleanup(out, udid):
    result = dict(simulatorID=udid, deleted=False, absent=False)
    for operation in ('shutdown', 'delete'):
        try:
            result[operation + 'ExitCode'] = sim([operation, udid], out, timeout=30, check=False).returncode
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            result[operation + 'Error'] = str(error)
    try:
        devices = json.loads(sim(['list', 'devices', '-j'], out, timeout=60).stdout)['devices']
        result['absent'] = all(row.get('udid') != udid for rows in devices.values() for row in rows)
        result['deleted'] = result.get('deleteExitCode') == 0 and result['absent']
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
        result['verificationError'] = str(error)
    write(out / 'cleanup.json', result)
    if not result['deleted']:
        raise RuntimeError('owned simulator cleanup incomplete')


def redact(out, inherited):
    for path in out.glob('*.log'):
        original = data = path.read_bytes()
        for entry in inherited.split(':'):
            if entry:
                data = data.replace(entry.encode(), b'<inherited-external-library>')
        if original == data:
            continue
        path.write_bytes(data)
        receipt = path.with_suffix('.receipt.json')
        if receipt.is_file():
            value = json.loads(receipt.read_text())
            value.update(unredactedTraceSHA256=value['traceSHA256'], externalLibraryPathsRedacted=True,
                         traceSHA256=hashlib.sha256(data).hexdigest())
            write(receipt, value)


def run(build, out):
    before = source_identity()
    info = validate_build(build)
    if any(key.startswith('SIMCTL_CHILD_CKSIM_') and value for key, value in os.environ.items()):
        raise RuntimeError('plain acceptance refuses inherited runtime overrides')
    out.mkdir(parents=True, exist_ok=False)
    rows = json.loads(sim(['list', 'runtimes', '-j'], out, timeout=60).stdout)['runtimes']
    available = [row for row in rows if row.get('identifier') == 'com.apple.CoreSimulator.SimRuntime.iOS-27-1'
                 and row.get('isAvailable') and row.get('platform') == 'iOS' and row.get('version') == '27.1']
    if len(available) != 1:
        raise RuntimeError('the exact iOS 27.1 simulator runtime is required')
    runtime = {key: available[0][key] for key in ('identifier', 'version', 'buildversion')}
    run_id = str(uuid.uuid4()).upper()
    name = 'expo-livekit-' + run_id
    udid = None
    inherited = ''
    created = False
    try:
        created = True
        candidate = sim(['create', name, 'com.apple.CoreSimulator.SimDeviceType.iPhone-Duo', runtime['identifier']],
                        out, timeout=30).stdout.decode().strip()
        uuid.UUID(candidate)
        udid = candidate
        sim(['boot', udid], out, timeout=60)
        sim(['bootstatus', udid, '-b'], out, timeout=300)
        if 'SIMCTL_CHILD_DYLD_INSERT_LIBRARIES' in os.environ:
            inherited = os.environ['SIMCTL_CHILD_DYLD_INSERT_LIBRARIES']
        else:
            query = sim(['spawn', udid, 'launchctl', 'getenv', 'DYLD_INSERT_LIBRARIES'], out, timeout=60, check=False)
            if query.returncode not in (0, 1):
                raise RuntimeError('cannot read inherited launch environment')
            inherited = query.stdout.removesuffix(b'\n').decode()
        if any(Path(path).name in ('CKSimRuntime.dylib', 'CKSimLoader.dylib') for path in inherited.split(':') if path):
            raise RuntimeError('external SDK runtime injection refused')
        sim(['install', udid, build / 'example.app'], out, timeout=180)
        installed_app = Path(sim(['get_app_container', udid, BUNDLE, 'app'], out, timeout=30).stdout.decode().strip())
        # Install once, query once, and reuse this owned app's container for both relaunches.
        data = Path(sim(['get_app_container', udid, BUNDLE, 'data'], out, timeout=30).stdout.decode().strip())
        if not installed_app.is_absolute() or not installed_app.is_dir() or not data.is_absolute() or not data.is_dir():
            raise RuntimeError('invalid installed app/data container')
        installed = {key: hashlib.sha256((installed_app / Path(key).relative_to('example.app')).read_bytes()).hexdigest()
                     for key in product_files(build) if key.startswith('example.app/')}
        if installed != {key: value for key, value in info['files'].items() if key.startswith('example.app/')}:
            raise RuntimeError('installed consumer bytes differ from bound build')
        sim(['privacy', udid, 'grant', 'microphone', BUNDLE], out)
        previous = None
        supervisors = {}
        for case in ('livekit', 'stale-audio'):
            previous, supervisors[case] = scenario(build, out, data, case, run_id, udid, inherited, previous)
    finally:
        # A second cancellation must not interrupt bounded owned-device cleanup.
        handlers = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            if created and not udid:
                devices = json.loads(sim(['list', 'devices', '-j'], out, timeout=60).stdout)['devices']
                owned = [row['udid'] for group in devices.values() for row in group if row.get('name') == name]
                if len(owned) > 1:
                    raise RuntimeError('ambiguous exact owned name; cleanup is unknown')
                udid = owned[0] if owned else None
            if udid:
                cleanup(out, udid)
        finally:
            try:
                redact(out, inherited)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
    if source_identity() != before:
        raise RuntimeError('source changed during native capture')
    write(out / 'capture.json', dict(schemaVersion=1, source=before, runID=run_id, simulatorID=udid,
          runtime=runtime, installed=installed, buildSHA256=hashlib.sha256((build / 'build.json').read_bytes()).hexdigest(),
          supervisorPIDs=supervisors, supervisorGroupsAbsent={case: True for case in supervisors},
          inheritedExternalDYLD=dict(present=bool(inherited), sha256=hashlib.sha256(inherited.encode()).hexdigest())))
    print(json.dumps(validate(build, out), indent=2))


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, handle_termination)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    run(args.build.resolve(), args.output.resolve())
