#!/usr/bin/env python3
"""Deliver a URL and typed intent directly on one owned simulator; no default-app dispatch."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import time
import uuid

import sdk_m0_conformance as m0
import sdk_m3_direct_conformance as acceptance_gate


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def probe_rows(path):
    rows = []
    if path.exists():
        for line in path.read_text(errors='replace').splitlines():
            if 'PROBE ' in line:
                try:
                    row = json.loads(line.split('PROBE ', 1)[1])
                    if isinstance(row, dict): rows.append(row)
                except ValueError:
                    pass
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--build-receipt', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True, help='fresh private capture directory')
    parser.add_argument('--runtime', help='installed iOS runtime identifier; otherwise latest available')
    parser.add_argument('--device-type', default='com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro')
    parser.add_argument('--preboot-wait-seconds', type=int, default=0,
                        help='bounded owned-UUID announcement checkpoint before boot')
    args = parser.parse_args()
    if not 0 <= args.preboot_wait_seconds <= 240: parser.error('Preboot checkpoint must be 0...240 seconds')
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    args.out.mkdir(mode=0o700)
    out = args.out.resolve()
    simulator = None
    creation_attempted = False
    supervisor = None
    control = None
    original_dyld = None
    dyld_sampled = False
    run_id = str(uuid.uuid4()).upper()
    owned_name = 'sdk-m3-' + run_id
    steps = []
    control_env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('SIMCTL_CHILD_CKSIM_', 'SIMCTL_CHILD_SDK_M0_', 'SIMCTL_CHILD_SDK_M1_', 'SIMCTL_CHILD_SDK_M2_', 'SIMCTL_CHILD_SDK_M3_', 'SIMCTL_CHILD_SDK_SYSTEM_'))}
    manifest = dict(schemaVersion=1, scenario='sdk-m3-direct', runID=run_id, ownsSimulator=True,
                    runtimeInjectionRequested=False, directIngressInjectionRequested=True,
                    delivery='direct-url-and-intent')
    failure = None
    def interrupted(signum, _frame):
        raise InterruptedError('M3 interrupted by signal ' + str(signum))
    saved_handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}

    def command(label, argv, timeout=60, env=None, check=True):
        started = time.monotonic()
        result = subprocess.run(argv, capture_output=True, timeout=timeout, env=control_env if env is None else env)
        steps.append(dict(phase=label, argv=argv, exitCode=result.returncode,
                          elapsedSeconds=round(time.monotonic() - started, 3),
                          stdoutSHA256=hashlib.sha256(result.stdout).hexdigest(),
                          stderrSHA256=hashlib.sha256(result.stderr).hexdigest()))
        # Control diagnostics are kept inside the private capture; environment
        # values and arbitrary system URL/intent payloads are never printed.
        if result.stderr: (out / (label + '.log')).write_bytes(result.stderr)
        if check and result.returncode: raise RuntimeError(label + ' failed; inspect the private capture')
        return result

    def sim(label, *argv, **kwargs):
        return command(label, ['xcrun', 'simctl', *argv], **kwargs)

    def read_dyld(label):
        result = sim(label, 'spawn', simulator, 'launchctl', 'getenv', 'DYLD_INSERT_LIBRARIES',
                     timeout=180, env={**control_env, 'SIMCTL_CHILD_DYLD_INSERT_LIBRARIES': ''}, check=False)
        if result.returncode not in (0, 1) or result.stderr or (result.returncode == 1 and result.stdout):
            raise RuntimeError('Cannot read simulator launch environment')
        return None if result.returncode == 1 else result.stdout.removesuffix(b'\n')

    def wait_for(predicate, seconds, message):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value: return value
            if supervisor.poll() is not None: raise RuntimeError('M3 supervisor exited before ' + message)
            time.sleep(0.05)
        raise RuntimeError('Deadline waiting for ' + message)

    try:
        identity = m0.source_identity()
        app = args.app.resolve()
        build = json.loads(args.build_receipt.read_text())
        acceptance_gate.check_input_build(app, build, identity)
        manifest.update(identity)
        shutil.copyfile(args.build_receipt, out / 'input-build-receipt.json')
        manifest['inputBuildReceiptSHA256'] = m0.digest(out / 'input-build-receipt.json')
        (out / 'binaries').mkdir()
        copied = out / 'example.app'
        shutil.copytree(app, copied, symlinks=True)
        command('verify-built-signature', ['codesign', '--verify', '--strict', str(copied)])
        host = command('read-host-entitlements', ['codesign', '--display', '--entitlements', ':-', str(copied)])
        host_claims = plistlib.loads(host.stdout)
        if not isinstance(host_claims, dict) or any(key in host_claims for key in
                ('com.apple.developer.calling-app', 'com.apple.developer.dialing-app')):
            raise RuntimeError('Restricted simulator claims must come from normal Xcode packaging, not host signing')
        (out / 'host-entitlements.plist').write_bytes(host.stdout)
        (out / 'simulated-entitlements.plist').write_bytes(acceptance_gate.simulated_entitlements(m0.executable(copied)))
        shutil.copyfile(m0.executable(copied), out / 'binaries/example')
        shutil.copyfile(copied / 'main.jsbundle', out / 'binaries/main.jsbundle')
        shutil.copyfile(copied / 'Info.plist', out / 'Info.plist')
        manifest['files'] = {name: m0.digest(out / name) for name in acceptance_gate.FILES}
        (out / 'build-logs').mkdir()
        if not build.get('buildLogs'): raise RuntimeError('Missing M3 build logs')
        for name, digest in build['buildLogs'].items():
            if Path(name).name != name: raise RuntimeError('Invalid build log filename')
            path = args.build_receipt.parent / name
            if path.stat().st_size == 0 or m0.digest(path) != digest: raise RuntimeError('Unbound or empty M3 build log')
            shutil.copyfile(path, out / 'build-logs' / name)
        # Preserve original app bytes; there is deliberately no post-build signing.
        acceptance_gate.check_input_build(app, build, identity)
        # Host metadata is independent of the DUT; collect it before simulator load.
        xcode_version = command('xcode-version', ['xcodebuild', '-version']).stdout.decode().strip()
        runtimes = json.loads(command('runtimes', ['xcrun', 'simctl', 'list', 'runtimes', '-j']).stdout)['runtimes']
        available = [r for r in runtimes if r.get('platform') == 'iOS' and r.get('isAvailable') and
                     (not args.runtime or r.get('identifier') == args.runtime)]
        if not available: raise RuntimeError('Requested iOS simulator runtime is unavailable')
        runtime = max(available, key=lambda r: tuple(int(p) for p in r['version'].split('.')))
        creation_attempted = True
        created = sim('create', 'create', owned_name, args.device_type, runtime['identifier']).stdout.decode().strip()
        uuid.UUID(created)
        simulator = created
        manifest['simulatorID'] = simulator
        print('Created owned M3 simulator ' + simulator + '; boot has not started.', flush=True)
        if args.preboot_wait_seconds:
            checkpoint = dict(runID=run_id, simulatorID=simulator, ownsSimulator=True)
            write(out / 'preboot-checkpoint.json', checkpoint)
            deadline = time.monotonic() + args.preboot_wait_seconds
            observation = out / 'boot-observation.json'
            while not observation.exists():
                if time.monotonic() >= deadline: raise RuntimeError('Deadline waiting for owned UUID announcement')
                time.sleep(0.05)
            if json.loads(observation.read_text()) != checkpoint:
                raise RuntimeError('Preboot observation does not match this owned simulator')
        sim('boot', 'boot', simulator)
        sim('bootstatus', 'bootstatus', simulator, '-b', timeout=300)
        original_dyld = read_dyld('read-original-dyld')
        dyld_sampled = True
        inherited = os.environ.get('SIMCTL_CHILD_DYLD_INSERT_LIBRARIES', (original_dyld or b'').decode())
        if any(Path(p).name in ('CKSimRuntime.dylib', 'CKSimLoader.dylib') for p in inherited.split(':') if p):
            raise RuntimeError('M3 refuses external SDK runtime injection')
        environment = dict(simulatorID=simulator, simulatorRuntimeID=runtime['identifier'],
                           simulatorOS=runtime['version'], simulatorBuild=runtime['buildversion'],
                           xcodeVersion=xcode_version)
        write(out / 'environment.json', environment)
        manifest['environmentSHA256'] = m0.digest(out / 'environment.json')
        sim('install', 'install', simulator, str(copied), timeout=180)
        installed = Path(sim('installed-app', 'get_app_container', simulator, acceptance_gate.BUNDLE, 'app').stdout.decode().strip())
        manifest['installed'] = dict(executableSHA256=m0.digest(m0.executable(installed)),
                                     javascriptSHA256=m0.digest(installed / 'main.jsbundle'),
                                     infoPlistSHA256=m0.digest(installed / 'Info.plist'))
        launch_env = dict(control_env)
        launch_env.update(SIMCTL_CHILD_DYLD_INSERT_LIBRARIES=inherited, SIMCTL_CHILD_CKSIM_RUN_ID=run_id,
                          SIMCTL_CHILD_SDK_M3_RUN_ID=run_id, SIMCTL_CHILD_SDK_M3_ACCEPTANCE='1')
        launch_env['SIMCTL_CHILD_SDK_M3_DELIVERY'] = 'direct-url-and-intent'
        control = (out / 'supervisor-control.log').open('wb')
        supervisor = subprocess.Popen([sys.executable, 'discovery/run_scenario.py', '--console-launcher-stop', '--inherit-launcher-group',
            '--timeout', '480', '--output', str(out / (acceptance_gate.NAME + '.log')),
            '--receipt', str(out / (acceptance_gate.NAME + '.receipt.json')), '--run-id', run_id, '--simulator-id', simulator,
            '--scenario', acceptance_gate.NAME, '--bundle-id', acceptance_gate.BUNDLE,
            '--terminate-command', 'xcrun', 'simctl', 'terminate', simulator, acceptance_gate.BUNDLE,
            '--launch-command', 'xcrun', 'simctl', 'launch', '--console', '--terminate-running-process', simulator, acceptance_gate.BUNDLE],
            env=launch_env, stdout=control, stderr=subprocess.STDOUT, start_new_session=True)
        manifest['supervisorPID'] = supervisor.pid
        trace = out / (acceptance_gate.NAME + '.log')
        ready = wait_for(lambda: next((r for r in probe_rows(trace) if r.get('event') == 'm3.listenerReady'), None),
                         60, 'the actual public listener')
        launch = next(r for r in probe_rows(trace) if r.get('event') == 'launch')
        if launch.get('runID') != run_id or launch.get('simulatorID') != simulator or ready.get('runID') != run_id or ready.get('simulatorID') != simulator or ready.get('pid') != launch.get('pid'):
            raise RuntimeError('M3 listener readiness has mixed launch identity')
        pid = launch['pid']
        data = Path(sim('app-data', 'get_app_container', simulator, acceptance_gate.BUNDLE, 'data').stdout.decode().strip())
        session = json.loads((data / 'Documents/.callkit-simulator/session.json').read_text())
        if any(session.get(k) != v for k, v in [('runID', run_id), ('pid', pid), ('bundleID', acceptance_gate.BUNDLE)]):
            raise RuntimeError('Runtime session does not match the launched example')
        control_value = dict(schemaVersion=1, runID=run_id, simulatorID=simulator, appPID=pid,
                             delivery='direct-url-and-intent')
        write(out / 'direct-control.json', control_value)
        manifest['directControlSHA256'] = m0.digest(out / 'direct-control.json')
        manifest['directControlCreatedWallTimeMS'] = time.time_ns() // 1_000_000
        destination = data / 'tmp' / ('sdk-m3-direct-' + run_id + '.json')
        temporary = destination.with_suffix('.tmp')
        shutil.copyfile(out / 'direct-control.json', temporary)
        os.replace(temporary, destination)
        if supervisor.wait(timeout=90): raise RuntimeError('No successful supervised public dialRequest; inspect eligibility and native ingress')
        if m0.source_identity() != identity: raise RuntimeError('M3 source changed during acceptance')
        acceptance_gate.check_input_build(app, build, identity)
    except (Exception, KeyboardInterrupt) as error:
        failure = dict(type=type(error).__name__, reason=str(error))
    finally:
        # A second cancellation must not skip bounded deletion of this run's
        # simulator. Restore the caller's handlers after owned cleanup completes.
        for sig in saved_handlers: signal.signal(sig, signal.SIG_IGN)
        try:
            cleanup = dict(simulatorID=simulator, deleted=False, supervisorCleanupRequired=False,
                           forcedGroupKill=False, supervisorGroupAbsentAfterStop=False)
            if supervisor is not None:
                cleanup['supervisorPID'] = supervisor.pid
                try:
                    if supervisor.poll() is None:
                        cleanup['supervisorCleanupRequired'] = True
                        failure = dict(reason='Supervisor failure cleanup cannot qualify M3', priorFailure=failure)
                        try: sim('stop-owned-example', 'terminate', simulator, acceptance_gate.BUNDLE, timeout=10, check=False)
                        except Exception as error: cleanup['appStopError'] = type(error).__name__
                        try: supervisor.wait(timeout=25)
                        except subprocess.TimeoutExpired:
                            try:
                                if supervisor.poll() is None:
                                    if os.getpgid(supervisor.pid) != supervisor.pid or os.getsid(supervisor.pid) != supervisor.pid:
                                        raise RuntimeError('Supervisor private process-group ownership is unknown')
                                    try:
                                        os.killpg(supervisor.pid, signal.SIGKILL)
                                        cleanup['forcedGroupKill'] = True
                                    except ProcessLookupError:
                                        pass
                            finally: supervisor.wait(timeout=5)
                    try: os.killpg(supervisor.pid, 0)
                    except ProcessLookupError: cleanup['supervisorGroupAbsentAfterStop'] = True
                    else: raise RuntimeError('Supervisor/console group remains; cleanup is unknown')
                except Exception as error:
                    cleanup['supervisorCleanupError'] = type(error).__name__
                    failure = dict(reason='Supervisor group cleanup is unknown', priorFailure=failure)
            if control is not None: control.close()
            owned = [simulator] if simulator else []
            if not simulator and creation_attempted:
                # CoreSimulator can create the device before its CLI times out.
                # Recover only this unpredictable exact name, never a QA simulator.
                try:
                    inventory = json.loads(sim('recover-created-simulator', 'list', 'devices', '-j').stdout)['devices']
                    owned = [str(uuid.UUID(row['udid'])).upper() for rows in inventory.values() for row in rows
                             if row.get('name') == owned_name]
                    cleanup['recoveredSimulators'] = owned
                except Exception as error:
                    owned = []
                    cleanup['creationRecoveryError'] = type(error).__name__
                    failure = dict(reason='Could not recover uncertain simulator creation', priorFailure=failure)
            if simulator:
                cleanup['globalEnvironmentUnchanged'] = False
                if dyld_sampled:
                    try:
                        restored = read_dyld('read-final-dyld')
                        cleanup['globalEnvironmentUnchanged'] = restored == original_dyld
                        if restored != original_dyld: failure = dict(reason='Simulator launch environment changed during M3', priorFailure=failure)
                    except Exception as error:
                        failure = dict(reason='Could not verify simulator launch environment', type=type(error).__name__, priorFailure=failure)
            removals = []
            for udid in owned:
                removal = dict(simulatorID=udid, deleted=False, absentFromInventory=False)
                try: sim('shutdown', 'shutdown', udid, timeout=60, check=False)
                except Exception: pass
                try:
                    deleted = sim('delete', 'delete', udid, timeout=60, check=False)
                    removal['deleteExitCode'] = deleted.returncode
                except Exception as error:
                    removal['errorType'] = type(error).__name__
                try:
                    inventory = json.loads(sim('verify-deletion', 'list', 'devices', '-j').stdout)['devices']
                    removal['absentFromInventory'] = all(row.get('udid') != udid for rows in inventory.values() for row in rows)
                    removal['deleted'] = removal.get('deleteExitCode') == 0 and removal['absentFromInventory']
                except Exception as error:
                    removal['verificationError'] = type(error).__name__
                removals.append(removal)
            if simulator and removals: cleanup.update(removals[0])
            elif owned: cleanup.update(deleted=all(row['deleted'] for row in removals), recoveredCleanup=removals)
            if owned and not cleanup['deleted']:
                failure = dict(reason='Owned simulator deletion did not complete', priorFailure=failure)
            write(out / 'cleanup.json', cleanup)
            write(out / 'commands.json', dict(steps=steps))
            manifest['cleanupSHA256'] = m0.digest(out / 'cleanup.json')
            manifest['commandsSHA256'] = m0.digest(out / 'commands.json')
            write(out / 'manifest.json', manifest)
        finally:
            for sig, handler in saved_handlers.items(): signal.signal(sig, handler)
    if failure is None:
        try: result = acceptance_gate.validate(out)
        except Exception as error: result = dict(success=False, error=str(error))
    else:
        result = dict(success=False, failure=failure, nativeM3Acceptance='pending')
    write(out / 'summary.json', result)
    print(json.dumps(dict(success=result['success'], capture=str(out), nativeM3Acceptance='measured' if result['success'] else 'pending')))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
