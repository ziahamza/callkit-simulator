#!/usr/bin/env python3
"""A0/A1 cases on the caller's fresh, owned M0 simulator; never boots one."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid

from check_push_report_entry import (native_source_identity, qualify_journal, qualify_negative_journal,
                                     qualify_deadline_journal, push_deadline_references)
from push_main_stall_conformance import qualify_main_stall_journal
from run_scenario import run
from summarize import successful_stop

BUNDLE = 'dev.iosvoipsdk.pushentry'
CASES = ('sdk-required', 'sdk-duplicate', 'missing', 'deferred', 'optional', 'sdk-main-stall', 'late-report')
CLASSIFICATION = 'simulator in-app PushKit injections and direct SDK callback-body main-stall fixture; not cold launch or APNs'
PRODUCTS = {'PushEntry.app/PushEntry', 'PushEntry.app/Info.plist',
            'PushEntry.app/IosVoipSDKRuntime.bundle/ios-27.0.json'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def simctl(out, label, *arguments):
    result = subprocess.run(['xcrun', 'simctl', *arguments], capture_output=True, timeout=60)
    (out / (label + '.stdout')).write_bytes(result.stdout)
    (out / (label + '.stderr')).write_bytes(result.stderr)
    require(result.returncode == 0, label + ' failed: ' + str(result.returncode))
    return result.stdout.decode().strip()


def validate(out):
    """Replay every normal/negative rule against the retained bound evidence."""
    manifest = json.loads((out / 'manifest.json').read_text())
    require(type(manifest.get('schemaVersion')) is int and manifest['schemaVersion'] == 1,
            'unsupported native capture manifest')
    require(manifest['source'] == native_source_identity(), 'capture source differs from checkout')
    require(manifest['classification'] == CLASSIFICATION,
            'incorrect acceptance scope')
    sim = str(uuid.UUID(manifest['simulatorID'])).upper()
    require(sim == manifest['simulatorID'], 'invalid simulator identity')
    require(set(manifest['cases']) == set(CASES), 'missing or extra native case')
    require(set(manifest['files']) == {str(path.relative_to(out)) for path in out.rglob('*')
            if path.is_file() and path not in (out / 'manifest.json', out / 'summary.json')},
            'missing or extra capture file binding')
    for name, expected in manifest['files'].items():
        require(digest(out / name) == expected, 'changed capture file: ' + name)
    build = json.loads((out / 'build/build-receipt.json').read_text())
    require(type(build.get('schemaVersion')) is int and build['schemaVersion'] == 1 and
            set(build['files']) == PRODUCTS and build['source'] == manifest['source'] and
            build.get('classification') == 'simulator-source-probe',
            'build and capture source or product kind differ')
    for name, expected in build['files'].items():
        require(digest(out / 'build' / name) == expected, 'built product changed: ' + name)
    installed = json.loads((out / 'installed.json').read_text())
    require(installed == build['files'], 'installed executable, Info or profile differs from build')
    profile = json.loads((out / 'build/PushEntry.app/IosVoipSDKRuntime.bundle/ios-27.0.json').read_text())
    require(profile.get('id') == 'ios-27.0' and type(profile.get('schemaVersion')) is int and
            profile['schemaVersion'] == 1 and profile == json.loads((Path(__file__).resolve().parents[1] /
                'Sources/Engine/Resources/ios-27.0.json').read_text()), 'incorrect runtime profile')
    results = []
    for mode in CASES:
        receipt = json.loads((out / (mode + '.receipt.json')).read_text())
        identity = manifest['cases'][mode]
        pid, run_id = identity['pid'], identity['runID']
        require(type(pid) is int and pid > 0 and str(uuid.UUID(run_id)) == run_id,
                'invalid native process or run identity')
        require(type(receipt.get('schemaVersion')) is int and receipt['schemaVersion'] == 2 and
                receipt.get('success') is True and not receipt.get('error') and
                receipt.get('appPID') == pid and receipt.get('launcherAppPID') == pid and
                receipt.get('runID') == run_id and receipt.get('simulatorID') == sim and
                receipt.get('bundleID') == BUNDLE and receipt.get('scenario') == 'sdk-a1-' + mode,
                'unbound or unsuccessful supervisor')
        if mode == 'late-report':
            require(receipt.get('expectedAppExit') is True and receipt.get('completed') is False and
                    receipt.get('terminationMethod') == 'natural-app-exit' and receipt.get('appAbsentBeforeCleanup') is True and
                    receipt.get('appAbsenceMethod') == 'signal-zero' and type(receipt.get('launchExitCode')) is int and
                    receipt['launchExitCode'] in (0, 78) and type(receipt.get('launchExitCodeBeforeCleanup')) is int and
                    receipt['launchExitCodeBeforeCleanup'] == receipt['launchExitCode'] and
                    not any(key in receipt for key in ('cleanupExitCode', 'cleanupError', 'terminateExitCode', 'terminationSignal', 'terminationSignalDelivered')) and
                    not successful_stop(receipt), 'expiry was not a natural, pre-cleanup app exit')
        else:
            require(receipt.get('completed') is True and receipt.get('terminationMethod') == 'console-launcher-signal' and
                    successful_stop(receipt), 'ordinary case lost successful owned stop')
        require(receipt.get('traceSHA256') == digest(out / (mode + '.log')) and
                receipt.get('journalSHA256') == digest(out / (mode + '.runtime.jsonl')),
                'unbound console or native journal')
        console = (out / (mode + '.log')).read_text()
        matches = re.findall(r'(?m)^' + re.escape(BUNDLE) + r': (\d+)\r?$', console)
        require(matches == [str(pid)], 'missing or conflicting launcher PID acknowledgement')
        probes = [json.loads(line.split('PROBE ', 1)[1]) for line in
                  console.splitlines() if 'PROBE ' in line]
        require(probes and all(type(row.get('pid')) is int and row['pid'] == pid and row.get('runID') == run_id and
                row.get('simulatorID') == sim and row.get('bundleID') == BUNDLE and row.get('main') is True
                for row in probes), 'mixed probe identity or unexpected probe executor')
        require([row.get('sequence') for row in probes] == list(range(1, len(probes) + 1)) and
                all(type(row.get('sequence')) is int and type(row.get('t_ms')) is int for row in probes) and
                all(a['t_ms'] <= b['t_ms'] for a, b in zip(probes, probes[1:])), 'invalid probe sequence')
        def one(event):
            rows = [row for row in probes if row.get('event') == event]
            require(len(rows) == 1, 'missing or repeated probe ' + event)
            return rows[0]
        launch = one('launch')
        require(probes[:2] == [one('app.entry'), launch], 'missing native startup evidence')
        require(launch.get('case') == mode and launch.get('executableSHA256') ==
                build['files']['PushEntry.app/PushEntry'], 'wrong launched native product')
        if mode == 'late-report':
            require(not any(row['event'] in ('entry.capture', 'done') for row in probes), 'expiry fixture reached normal completion')
        else:
            require(one('entry.capture').get('boundariesObserved') is True and probes[-1] == one('done'),
                    'missing terminal native boundary observation')
        session = json.loads((out / (mode + '.session.json')).read_text())
        require(type(session.get('pid')) is int and session['pid'] == pid and session.get('runID') == run_id and
                session.get('bundleID') == BUNDLE and type(session.get('schemaVersion')) is int and
                session['schemaVersion'] == 1 and
                session.get('profile') == profile['id'] and
                session.get('notification') == 'dev.callkitsim.control.' + str(pid), 'unbound runtime session')
        rows = [json.loads(line) for line in (out / (mode + '.runtime.jsonl')).read_text().splitlines()]
        require(rows and all(row.get('profile') == profile['id'] for row in rows), 'mixed native runtime profile')
        if mode == 'late-report':
            results.append(qualify_deadline_journal(rows, run_id, pid, profile))
        elif mode == 'sdk-main-stall':
            results.append(qualify_main_stall_journal(rows, probes, run_id, pid))
        else:
            gate = qualify_negative_journal if mode in ('missing', 'deferred') else qualify_journal
            results.append(gate(rows, mode, run_id, pid))
    require(len({item['pid'] for item in manifest['cases'].values()}) == len(CASES) and
            len({item['runID'] for item in manifest['cases'].values()}) == len(CASES),
            'native cases reused a process or run')
    return dict(success=True, classification=manifest['classification'], source=manifest['source'],
                simulatorID=sim, deviceReferences=push_deadline_references(profile), cases=results,
                cleanupResponsibility='The enclosing M0 runner must record owned simulator deletion.')


def capture(build, out, sim):
    sim = str(uuid.UUID(sim)).upper()
    out.mkdir()
    shutil.copytree(build, out / 'build')
    identity = native_source_identity()
    build_receipt = json.loads((out / 'build/build-receipt.json').read_text())
    require(build_receipt['source'] == identity, 'source changed after native build')
    for name, expected in build_receipt['files'].items():
        require(digest(out / 'build' / name) == expected, 'built product changed: ' + name)
    app = out / 'build/PushEntry.app'
    simctl(out, 'install', 'install', sim, str(app))
    installed_app = Path(simctl(out, 'installed-container', 'get_app_container', sim, BUNDLE, 'app'))
    installed = {name: digest(installed_app / Path(name).relative_to('PushEntry.app'))
                 for name in build_receipt['files']}
    save(out / 'installed.json', installed)
    inherited = os.environ.get('SIMCTL_CHILD_DYLD_INSERT_LIBRARIES')
    require(inherited is not None, 'M0 must supply its preserved inherited launch environment')
    identities = {}
    for mode in CASES:
        run_id = str(uuid.uuid4())
        log, receipt = out / (mode + '.log'), out / (mode + '.receipt.json')
        launch = ['env', *[part for key in sorted(os.environ) if key.startswith('SIMCTL_CHILD_')
                          for part in ('-u', key)], 'SIMCTL_CHILD_DYLD_INSERT_LIBRARIES=' + inherited,
                  'SIMCTL_CHILD_CKSIM_RUN_ID=' + run_id, 'SIMCTL_CHILD_SDK_PUSH_ENTRY_CASE=' + mode,
                  'xcrun', 'simctl', 'launch', '--console', sim, BUNDLE]
        status = run(launch, ['xcrun', 'simctl', 'terminate', sim, BUNDLE], log, receipt, 30,
                     dict(runID=run_id, simulatorID=sim, scenario='sdk-a1-' + mode, bundleID=BUNDLE), True,
                     expected_app_exit=(mode == 'late-report'))
        require(status == 0, mode + ' supervisor failed; retained receipt explains failure')
        supervisor = json.loads(receipt.read_text())
        container = Path(simctl(out, mode + '-container', 'get_app_container', sim, BUNDLE, 'data'))
        runtime = container / 'Documents/.callkit-simulator'
        shutil.copyfile(runtime / run_id / 'events.jsonl', out / (mode + '.runtime.jsonl'))
        shutil.copyfile(runtime / 'session.json', out / (mode + '.session.json'))
        # External devtools paths are unrelated to acceptance. Redact before
        # binding the console, retaining the original digest in its receipt.
        raw = log.read_bytes()
        redacted = raw
        for path in filter(None, inherited.split(':')):
            redacted = redacted.replace(path.encode(), b'<inherited-external-library>')
        if redacted != raw:
            log.write_bytes(redacted)
            supervisor.update(unredactedTraceSHA256=supervisor['traceSHA256'],
                              externalLibraryPathsRedacted=True, traceSHA256=digest(log))
        supervisor['journalSHA256'] = digest(out / (mode + '.runtime.jsonl'))
        save(receipt, supervisor)
        identities[mode] = dict(runID=run_id, pid=supervisor['appPID'])
    require(native_source_identity() == identity, 'source changed during native capture')
    save(out / 'manifest.json', dict(schemaVersion=1, source=identity, simulatorID=sim, cases=identities,
         classification=CLASSIFICATION,
         files={str(path.relative_to(out)): digest(path) for path in out.rglob('*') if path.is_file()}))
    result = validate(out)
    save(out / 'summary.json', result)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate', type=Path)
    parser.add_argument('--build', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--simulator')
    args = parser.parse_args()
    if args.validate:
        require(not any((args.build, args.out, args.simulator)), 'validate takes no capture arguments')
        print(json.dumps(validate(args.validate), indent=2))
    else:
        require(all((args.build, args.out, args.simulator)), 'provide build, out and owned simulator')
        capture(args.build.resolve(), args.out.resolve(), args.simulator)
