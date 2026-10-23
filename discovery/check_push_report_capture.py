#!/usr/bin/env python3
"""Synthetic complete-capture tamper controls; never native acceptance."""
from pathlib import Path
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

import run_push_report_entry as gate
from check_push_main_stall import synthetic_fixture

SOURCE = dict(sourceCommit='host-fixture', sourceInputsSHA256='host-fixture', inputs=['host-fixture'])
SIMULATOR = '21974CC4-7C06-43E3-8F35-EEDCAF325739'
CALL = 'E715ACF2-A7C1-4D74-8BD9-EAFA47693255'


def fixture(root):
    build = root / 'build'
    build.mkdir()
    files = {}
    profile = (Path(__file__).resolve().parents[1] / 'Sources/Engine/Resources/ios-27.0.json').read_bytes()
    for name, contents in [('PushEntry.app/PushEntry', b'host-only executable fixture'),
                           ('PushEntry.app/Info.plist', b'host-only Info fixture'),
                           ('PushEntry.app/IosVoipSDKRuntime.bundle/ios-27.0.json', profile)]:
        path = build / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        files[name] = gate.digest(path)
    gate.save(build / 'build-receipt.json', dict(schemaVersion=1, source=SOURCE, classification='simulator-source-probe', files=files))
    gate.save(root / 'installed.json', files)
    identities = {}
    for index, mode in enumerate(gate.CASES):
        run_id = f'00000000-0000-0000-0000-{index + 1:012d}'
        pid = 1000 + index
        identities[mode] = dict(runID=run_id, pid=pid)
        common = dict(pid=pid, runID=run_id, bundleID=gate.BUNDLE)
        events = []
        def row(event, **fields):
            events.append(dict(common, event=event, sequence=len(events) + 1, t_ms=len(events), profile='ios-27.0', **fields))
        for delivery in range(0 if mode == 'sdk-main-stall' else 2 if mode == 'sdk-duplicate' else 1):
            push = dict(pushID=run_id + '-' + str(delivery))
            boundary = dict(push, api='metadata', mustReport=mode != 'optional')
            violation = dict(push, severity='error')
            row('push.received', **boundary)
            if mode != 'optional':
                row('push.reportDeadline', **push, seconds=4.4)
            if mode == 'late-report':
                row('push.completed', **boundary, reportEntered=False)
                row('diagnostic.pushReportingViolation', **violation, reason='required-report-missing-at-completion')
                row('push.callbackReturned', **boundary, reportEntered=False)
                row('diagnostic.pushReportingViolation', **violation, reason='required-report-missing-at-callback-return')
                row('diagnostic.pushReportExpired', **violation, seconds=4.4, exitCode=78)
                events[-1]['t_ms'] = 4400
                continue
            if mode == 'missing':
                row('push.completed', **boundary, reportEntered=False)
                row('diagnostic.pushReportingViolation', **violation, reason='required-report-missing-at-completion')
                row('push.callbackReturned', **boundary, reportEntered=False)
                row('diagnostic.pushReportingViolation', **violation, reason='required-report-missing-at-callback-return')
                continue
            if mode == 'deferred':
                row('push.callbackReturned', **boundary, reportEntered=False)
                row('diagnostic.pushReportingViolation', **violation, reason='required-report-missing-at-callback-return')
            if mode != 'optional':
                row('report.incoming', **push, callUUID=CALL)
                row('report.completed', **push, callUUID=CALL, error=2 if delivery else 0)
            if mode != 'deferred':
                row('push.callbackReturned', **boundary, reportEntered=mode != 'optional')
            row('push.completed', **boundary, reportEntered=mode != 'optional')
        observation = None
        if mode == 'sdk-main-stall':
            events, observation = synthetic_fixture(run_id, pid)
        journal = root / (mode + '.runtime.jsonl')
        journal.write_text(''.join(json.dumps(row) + '\n' for row in events))
        gate.save(root / (mode + '.session.json'), dict(common, schemaVersion=1, profile='ios-27.0',
                  notification='dev.callkitsim.control.' + str(pid)))
        fields = [dict(event='app.entry'), dict(event='launch', case=mode,
                  executableSHA256=files['PushEntry.app/PushEntry'])]
        if observation: fields.append(observation)
        fields += ([dict(event='entry.inject', pushID=run_id + '-0', mustReport=True)] if mode == 'late-report' else
                   [dict(event='entry.capture', boundariesObserved=True), dict(event='done')])
        probes = [dict(item, sequence=i + 1, t_ms=i, main=True, simulatorID=SIMULATOR, **common)
                  for i, item in enumerate(fields)]
        log = root / (mode + '.log')
        log.write_text(gate.BUNDLE + ': ' + str(pid) + '\n' + ''.join('PROBE ' + json.dumps(row) + '\n' for row in probes))
        receipt = dict(common, schemaVersion=2, simulatorID=SIMULATOR, appPID=pid,
                  launcherAppPID=pid, success=True, completed=True, scenario='sdk-a1-' + mode,
                  launcherPID=2000 + index, launchExitCode=-15, terminationMethod='console-launcher-signal',
                  terminationSignal=15, terminationSignalDelivered=True, lastProbeEvent='done',
                  consoleTransport='host-pty', appAbsenceMethod='signal-zero', appAbsentAfterStop=True,
                  traceSHA256=gate.digest(log), journalSHA256=gate.digest(journal))
        if mode == 'late-report':
            for key in ('terminationSignal', 'terminationSignalDelivered', 'appAbsentAfterStop'):
                receipt.pop(key)
            receipt.update(completed=False, expectedAppExit=True, terminationMethod='natural-app-exit',
                           appAbsentBeforeCleanup=True, launchExitCode=0, launchExitCodeBeforeCleanup=0,
                           lastProbeEvent='entry.inject')
        gate.save(root / (mode + '.receipt.json'), receipt)
    gate.save(root / 'manifest.json', dict(schemaVersion=1, source=SOURCE, simulatorID=SIMULATOR, cases=identities,
              classification=gate.CLASSIFICATION,
              files={str(p.relative_to(root)): gate.digest(p) for p in root.rglob('*') if p.is_file()}))


class CaptureControls(unittest.TestCase):
    def test_required_deadline_binding_and_optional_omission(self):
        def deadline(rows):
            return next(row for row in rows if row['event'] == 'push.reportDeadline')

        def after_boundary(rows):
            value = deadline(rows)
            rows.remove(value)
            boundary = next(row for row in rows if row['event'] in ('push.completed', 'push.callbackReturned'))
            rows.insert(rows.index(boundary) + 1, value)

        mutations = [
            ('missing', lambda rows: rows.remove(deadline(rows)), 'deadline'),
            ('duplicate', lambda rows: rows.insert(2, dict(deadline(rows))), 'deadline'),
            ('foreign-push', lambda rows: deadline(rows).update(pushID='other'), 'deadline'),
            ('foreign-run', lambda rows: deadline(rows).update(runID='other'), 'identity'),
            ('foreign-process', lambda rows: deadline(rows).update(pid=9999), 'identity'),
            ('wrong-duration', lambda rows: deadline(rows).update(seconds=4.3), 'deadline'),
            ('boolean-duration', lambda rows: deadline(rows).update(seconds=True), 'deadline'),
            ('after-boundary', after_boundary, 'deadline'),
        ]
        for mode in ('missing', 'deferred', 'optional'):
            cases = mutations if mode != 'optional' else [('unexpected', None, 'deadline')]
            for label, mutate, error in cases:
                with self.subTest(mode=mode, deadline=label), tempfile.TemporaryDirectory(prefix='push-deadline-binding-') as temporary:
                    root = Path(temporary)
                    fixture(root)
                    with patch.object(gate, 'native_source_identity', return_value=SOURCE):
                        self.assertTrue(gate.validate(root)['success'], 'The intact shipping-shaped capture must pass first')
                    path = root / (mode + '.runtime.jsonl')
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    if mode == 'optional':
                        fields = {key: rows[0][key] for key in ('pid', 'runID', 'bundleID', 'profile', 'pushID')}
                        rows.insert(1, dict(fields, event='push.reportDeadline', seconds=4.4))
                    else:
                        mutate(rows)
                    for index, row in enumerate(rows): row.update(sequence=index + 1, t_ms=index)
                    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                    receipt_path = root / (mode + '.receipt.json')
                    receipt = json.loads(receipt_path.read_text())
                    receipt['journalSHA256'] = gate.digest(path)
                    gate.save(receipt_path, receipt)
                    manifest = json.loads((root / 'manifest.json').read_text())
                    for changed in (path, receipt_path): manifest['files'][changed.name] = gate.digest(changed)
                    gate.save(root / 'manifest.json', manifest)
                    with patch.object(gate, 'native_source_identity', return_value=SOURCE):
                        with self.assertRaisesRegex(ValueError, error): gate.validate(root)

    def test_rebound_deadline_capture_refusals(self):
        def actual_report(rows):
            rows.insert(1, dict(rows[0], event='report.incoming', callUUID=CALL))
            for index, row in enumerate(rows): row['sequence'] = index + 1

        def unexpected_done(rows):
            rows.append(dict(rows[-1], event='done', sequence=len(rows) + 1, t_ms=rows[-1]['t_ms'] + 1))

        cases = [
            ('receipt', 'cleanup-assisted', lambda value: value.update(cleanupExitCode=0), 'natural, pre-cleanup'),
            ('receipt', 'cleanup-error', lambda value: value.update(cleanupError='failed cleanup'), 'natural, pre-cleanup'),
            ('receipt', 'signal-assisted', lambda value: value.update(terminationSignal=15, terminationSignalDelivered=True), 'natural, pre-cleanup'),
            ('receipt', 'command-assisted', lambda value: value.update(terminateExitCode=0), 'natural, pre-cleanup'),
            ('receipt', 'absence-not-observed', lambda value: value.update(appAbsentBeforeCleanup=False), 'natural, pre-cleanup'),
            ('receipt', 'pre-cleanup-status-absent', lambda value: value.pop('launchExitCodeBeforeCleanup'), 'natural, pre-cleanup'),
            ('receipt', 'launcher-status-substituted', lambda value: value.update(launchExitCode=-15), 'natural, pre-cleanup'),
            ('receipt', 'boolean-launcher-status', lambda value: value.update(launchExitCode=False), 'natural, pre-cleanup'),
            ('console', 'unexpected-done', unexpected_done, 'normal completion'),
            ('journal', 'missing-expiry', lambda value: value.pop(), 'diagnostic.pushReportExpired'),
            ('journal', 'foreign-expiry', lambda value: value[-1].update(pushID='other-push'), 'diagnostic.pushReportExpired'),
            ('journal', 'early-expiry', lambda value: value[-1].update(t_ms=4399), 'planned late report'),
            ('journal', 'duplicate-expiry', lambda value: value.append(dict(value[-1], sequence=len(value) + 1)), 'diagnostic.pushReportExpired'),
            ('journal', 'actual-report', actual_report, 'native report escaped'),
            ('journal', 'expiry-exit-substituted', lambda value: value[-1].update(exitCode=0), 'exit policy'),
            ('journal', 'boolean-journal-sequence', lambda value: value[0].update(sequence=True), 'clock/sequence'),
            ('journal', 'float-journal-pid', lambda value: value[0].update(pid=float(value[0]['pid'])), 'identity'),
        ]
        for kind, label, mutate, error in cases:
            with self.subTest(rebound=label), tempfile.TemporaryDirectory(prefix='push-deadline-host-') as temporary:
                root = Path(temporary)
                fixture(root)
                manifest = json.loads((root / 'manifest.json').read_text())
                receipt_path = root / 'late-report.receipt.json'
                receipt = json.loads(receipt_path.read_text())
                path = receipt_path
                if kind == 'receipt':
                    mutate(receipt)
                else:
                    path = root / ('late-report.log' if kind == 'console' else 'late-report.runtime.jsonl')
                    if kind == 'console':
                        lines = path.read_text().splitlines()
                        rows = [json.loads(line.split('PROBE ', 1)[1]) for line in lines if 'PROBE ' in line]
                        mutate(rows)
                        path.write_text(lines[0] + '\n' + ''.join('PROBE ' + json.dumps(row) + '\n' for row in rows))
                    else:
                        rows = [json.loads(line) for line in path.read_text().splitlines()]
                        mutate(rows)
                        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                    receipt['traceSHA256' if kind == 'console' else 'journalSHA256'] = gate.digest(path)
                gate.save(receipt_path, receipt)
                for changed in (path, receipt_path):
                    manifest['files'][changed.name] = gate.digest(changed)
                gate.save(root / 'manifest.json', manifest)
                with patch.object(gate, 'native_source_identity', return_value=SOURCE):
                    with self.assertRaisesRegex(ValueError, error): gate.validate(root)

    def test_rebound_main_stall_evidence_cannot_substitute_for_actual_entry(self):
        for tamper in ('remove-report', 'replace-public-payload'):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory(prefix='push-stall-host-') as temporary:
                root = Path(temporary)
                fixture(root)
                manifest = json.loads((root / 'manifest.json').read_text())
                receipt = json.loads((root / 'sdk-main-stall.receipt.json').read_text())
                if tamper == 'remove-report':
                    path = root / 'sdk-main-stall.runtime.jsonl'
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    rows = [row for row in rows if row['event'] != 'report.incoming']
                    for index, row in enumerate(rows): row['sequence'] = index + 1
                    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                    receipt['journalSHA256'] = gate.digest(path)
                else:
                    path = root / 'sdk-main-stall.log'
                    path.write_text(path.read_text().replace('main-stall-server', 'substituted-server'))
                    receipt['traceSHA256'] = gate.digest(path)
                gate.save(root / 'sdk-main-stall.receipt.json', receipt)
                for name in ('sdk-main-stall.receipt.json', str(path.relative_to(root))):
                    manifest['files'][name] = gate.digest(root / name)
                gate.save(root / 'manifest.json', manifest)
                with patch.object(gate, 'native_source_identity', return_value=SOURCE):
                    with self.assertRaises(ValueError): gate.validate(root)

    def test_complete_capture_and_tamper_refusals(self):
        with tempfile.TemporaryDirectory(prefix='push-entry-host-') as temporary:
            root = Path(temporary)
            fixture(root)
            with patch.object(gate, 'native_source_identity', return_value=SOURCE):
                self.assertTrue(gate.validate(root)['success'])
                original = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                for name in ('sdk-required.runtime.jsonl', 'deferred.receipt.json', 'missing.session.json',
                             'optional.log', 'build/build-receipt.json', 'installed.json'):
                    with self.subTest(missing=name):
                        (root / name).unlink()
                        with self.assertRaises((ValueError, OSError)): gate.validate(root)
                        (root / name).write_bytes(original[name])
                for name in original:
                    if name == 'manifest.json': continue
                    with self.subTest(empty=name):
                        (root / name).write_bytes(b'')
                        with self.assertRaises((ValueError, OSError)): gate.validate(root)
                        (root / name).write_bytes(original[name])
                manifest = json.loads(original['manifest.json'])
                for label, mutate in [
                    ('wrong-schema', lambda m: m.update(schemaVersion=True)),
                    ('missing-case', lambda m: m['cases'].pop('deferred')),
                    ('extra-case', lambda m: m['cases'].update(extra=m['cases']['optional'])),
                    ('unbound-file', lambda m: m['files'].pop('optional.log')),
                    ('wrong-source', lambda m: m['source'].update(sourceCommit='other')),
                    ('reused-pid', lambda m: m['cases']['optional'].update(pid=1000)),
                    ('reused-run', lambda m: m['cases']['optional'].update(runID=m['cases']['sdk-required']['runID'])),
                ]:
                    with self.subTest(tamper=label):
                        changed = copy.deepcopy(manifest); mutate(changed)
                        gate.save(root / 'manifest.json', changed)
                        with self.assertRaises(ValueError): gate.validate(root)
                (root / 'manifest.json').write_bytes(original['manifest.json'])
                for name, label, mutate in [
                    ('build/build-receipt.json', 'omitted-profile', lambda r: r['files'].pop('PushEntry.app/IosVoipSDKRuntime.bundle/ios-27.0.json')),
                    ('build/build-receipt.json', 'omitted-Info', lambda r: r['files'].pop('PushEntry.app/Info.plist')),
                    ('build/build-receipt.json', 'wrong-build-kind', lambda r: r.update(classification='other')),
                    ('build/build-receipt.json', 'wrong-build-schema', lambda r: r.update(schemaVersion=True)),
                    ('deferred.session.json', 'wrong-profile', lambda r: r.update(profile='other')),
                    ('deferred.session.json', 'wrong-notification', lambda r: r.update(notification='dev.callkitsim.control.1')),
                    ('deferred.session.json', 'wrong-session-run', lambda r: r.update(runID='other')),
                    ('sdk-required.receipt.json', 'missing-stop-method', lambda r: r.pop('terminationMethod')),
                    ('sdk-required.receipt.json', 'missing-launch-exit', lambda r: r.pop('launchExitCode')),
                    ('sdk-required.receipt.json', 'failed-launch-exit', lambda r: r.update(launchExitCode=1)),
                    ('sdk-required.receipt.json', 'missing-stop-signal', lambda r: r.pop('terminationSignal')),
                    ('sdk-required.receipt.json', 'undelivered-stop-signal', lambda r: r.update(terminationSignalDelivered=False)),
                    ('sdk-required.receipt.json', 'missing-launcher-pid', lambda r: r.pop('launcherPID')),
                    ('sdk-required.receipt.json', 'cleanup-error', lambda r: r.update(cleanupError='failed stop')),
                    ('sdk-required.receipt.json', 'wrong-stop-shape', lambda r: r.update(terminationMethod='command', terminateExitCode=0)),
                ]:
                    with self.subTest(rebound=label):
                        changed = json.loads(original[name]); mutate(changed)
                        gate.save(root / name, changed)
                        rebound = copy.deepcopy(manifest)
                        rebound['files'][name] = gate.digest(root / name)
                        gate.save(root / 'manifest.json', rebound)
                        with self.assertRaises(ValueError): gate.validate(root)
                        (root / name).write_bytes(original[name])
                (root / 'manifest.json').write_bytes(original['manifest.json'])
                # Rebinding a derived receipt cannot replace the native launch acknowledgement.
                path = root / 'sdk-required.log'
                path.write_text(path.read_text().replace(gate.BUNDLE + ': 1000', gate.BUNDLE + ': 9999'))
                receipt = json.loads(original['sdk-required.receipt.json'])
                receipt['traceSHA256'] = gate.digest(path)
                gate.save(root / 'sdk-required.receipt.json', receipt)
                manifest['files']['sdk-required.log'] = gate.digest(path)
                manifest['files']['sdk-required.receipt.json'] = gate.digest(root / 'sdk-required.receipt.json')
                gate.save(root / 'manifest.json', manifest)
                with self.assertRaisesRegex(ValueError, 'launcher PID'): gate.validate(root)


if __name__ == '__main__':
    unittest.main()
