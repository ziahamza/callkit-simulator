#!/usr/bin/env python3
"""Synthetic logical-gate controls; no native acceptance is claimed here."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import uuid

import check_sdk_m1 as legacy
import sdk_system_conformance as gate

WALL = 1792627200000
SESSION = dict(schemaVersion=1, runID='synthetic-system-run', pid=12345, bundleID=gate.BUNDLE,
               profile='ios-27.0', notification='dev.callkitsim.control.12345')
SERVICES = 'PID\tStatus\tLabel\n-\t0\tUIKitApplication:' + gate.BUNDLE + '[synthetic-stopped]\n'


def iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


class Fixture:
    def __init__(self, case, expected, report_start_ms=1):
        self.case, self.expected = case, expected
        self.report_start_ms = report_start_ms
        self.rows, self.engine, self.controls, self.commands = [], [], [], []
        self.index = dict(bindings=[], controls=[], fixtureCommands=[])
        self.calls, self.clock, self.serial, self.revision = {}, 0, 0, 0
        self.launch_receipt = None
        self.app('app.entry')
        self.app('launch', mode='sdk-system', case=case, traceVersion=2)
        self.app('sdk.activation', active=True)
        self.app('sdk.managerCreated', owner='VoIPApplication.start')
        self.snapshot()
        self.hook()
        if case != 'push-launch': self.app('system.handlerAttached', attached=True)
        self.native('runtime.installed', version='synthetic')
        self.native('provider.created')
        clock = self.native('system.clock')['t_ms']
        self.app('system.ready', attached=case != 'push-launch', clockBeforeMS=WALL + clock)

    def uuid(self):
        self.serial += 1
        return str(uuid.UUID(int=self.serial)).upper()

    def app(self, event, **fields):
        self.clock += 1
        row = dict(event=event, sequence=len(self.rows) + 1, t_ms=self.clock, wallTimeMS=WALL + self.clock,
                   main=True, runID=SESSION['runID'], pid=SESSION['pid'], bundleID=gate.BUNDLE,
                   simulatorID='synthetic-simulator', **fields)
        self.rows.append(row)
        return row

    def native(self, event, **fields):
        self.clock += 1
        row = dict(event=event, sequence=len(self.engine) + 1, t_ms=self.clock,
                   runID=SESSION['runID'], pid=SESSION['pid'], bundleID=gate.BUNDLE,
                   profile='ios-27.0', provider='synthetic-provider', **fields)
        self.engine.append(row)
        return row

    def snapshot(self):
        row = self.app('sdk.snapshot', revision=self.revision, conversations=deepcopy(list(self.calls.values())))
        self.revision += 1
        return row

    def hook(self, binding=None):
        call = self.calls.get(binding['conversationID']) if binding else None
        self.app('system.hook', count=len(self.calls), conversationID=call['id'] if call else None,
                 status=call['status'] if call else None, hasConnectedAt=bool(call and call['hasConnectedAt']),
                 muted=bool(call and call['isMuted']))

    def control(self, operation, **fields):
        request = dict((key, SESSION[key]) for key in ('runID', 'pid', 'bundleID'))
        request.update(id=self.uuid(), operation=operation, **fields)
        control = dict(schemaVersion=1, session=SESSION.copy(), request=request,
                       response=dict((key, request[key]) for key in ('runID', 'pid', 'bundleID', 'id')))
        control['response'].update(success=True, result={})
        self.index['controls'].append(f'control-{len(self.controls):02}.json')
        self.controls.append(control)
        return control

    def command(self, operation, binding=None, body=None):
        command = dict(id=self.uuid(), runID=SESSION['runID'], case=self.case, pid=SESSION['pid'], operation=operation)
        if binding: command['conversationID'] = binding['conversationID']
        self.index['fixtureCommands'].append(f'fixture-{len(self.commands):02}.json')
        self.commands.append(command)
        self.app('system.commandReceived', id=command['id'], operation=operation)
        if body: body(command)
        self.app('system.commandCompleted', id=command['id'], operation=operation)
        return command

    def push(self, role, duplicate=False, cold=False):
        payload = dict(incomingCall=dict(eventId='synthetic-' + role, serverCallId='synthetic-server-' + role))
        if cold:
            push_id = self.uuid()
            envelope = dict(schemaVersion=1, id=push_id, runID=SESSION['runID'], bundleID=gate.BUNDLE,
                            createdAtMS=WALL, expiresAtMS=WALL + 30000, mustReport=True, payload=payload)
            digest = hashlib.sha256((json.dumps(envelope, sort_keys=True) + '\n').encode()).hexdigest()
            self.launch_receipt = dict(schemaVersion=1, simulatorID='synthetic-simulator', bundleID=gate.BUNDLE,
                runID=SESSION['runID'], pushID=push_id, envelope=envelope, envelopeSHA256=digest,
                mode=0o600, appServiceAbsent=True, runtimeInjectionRequested=False,
                serviceQuery=SERVICES, serviceQuerySHA256=hashlib.sha256(SERVICES.encode()).hexdigest())
            self.native('push.launchConsumed', pushID=push_id, envelopeSHA256=digest,
                        createdAtMS=WALL, expiresAtMS=WALL + 30000)
        else:
            control = self.control('push', payload=payload, mustReport=not duplicate)
            push_id = control['request']['id']
            control['response']['result'] = dict(scheduled=True, pushID=push_id)
        self.native('push.received', pushID=push_id, api='metadata', mustReport=not duplicate)
        if not duplicate:
            binding = dict(role=role, conversationID=self.uuid(), callUUID=self.uuid(), pushID=push_id)
            self.clock += self.report_start_ms - 1
            report = self.native('report.incoming', pushID=push_id, callUUID=binding['callUUID'])
            self.calls[binding['conversationID']] = dict(id=binding['conversationID'], direction='incoming', status='ringing',
                                                       isMuted=False, isVideo=False, hasConnectedAt=False)
            snapshot = self.snapshot()
            binding.update(reportSequence=report['sequence'], snapshotSequence=snapshot['sequence'])
            self.index['bindings'].append(binding)
        else:
            binding = self.index['bindings'][0]
            self.native('report.incoming', pushID=push_id, callUUID=binding['callUUID'])
        self.native('push.callbackReturned', pushID=push_id, mustReport=not duplicate, reportEntered=True)
        self.native('report.completed', pushID=push_id, callUUID=binding['callUUID'], error=2 if duplicate else 0)
        self.native('push.completed', pushID=push_id, mustReport=not duplicate, reportEntered=True)
        return None if duplicate else binding

    def transaction(self, specs, control=None, origin='system', signal=True):
        ids = [self.uuid() for _ in specs]
        self.native('transaction.delivered', transactionID=self.uuid(), requestID=control['request']['id'] if control else origin,
                    origin=origin, actionIDs=ids, kinds=[gate.NATIVE[kind] for kind, _, _ in specs],
                    callUUIDs=[binding['callUUID'] for _, binding, _ in specs], parameters=[params for _, _, params in specs])
        if control: control['response']['result'] = dict(actionIDs=ids)
        actions = []
        for identity, (kind, binding, params) in zip(ids, specs):
            deadline = self.native('action.deadline', actionID=identity, apiKind='livecommunicationkit',
                                   deadlineMS=WALL + self.clock + 30000, deadlineSeconds=30, expirySeconds=30)
            action = dict(id=identity, kind=kind, binding=binding, params=params, origin=origin,
                          deadline=iso(deadline['deadlineMS'] - 1500), handled=False)
            actions.append(action)
            if signal: self.handler(action)
        return actions

    def handler(self, action):
        action['handled'] = True
        kind, binding, origin = action['kind'], action['binding'], action['origin']
        origin = 'app' if origin == 'application' else origin
        call = self.calls[binding['conversationID']]
        call['pending'] = dict(id=self.uuid(), kind=kind, origin=origin, deadline=action['deadline'])
        if kind == 'join': call['status'] = 'connecting'
        if kind == 'end': call['status'] = 'ending'
        self.snapshot()
        row = self.app('system.handler', kind=kind, conversationID=binding['conversationID'], origin=origin,
                       deadline=action['deadline'], **action['params'])
        self.native('system.handlerEntered', traceSequence=row['sequence'], kind=kind,
                    conversationID=binding['conversationID'], origin=origin, deadline=action['deadline'], **action['params'])

    def settle(self, actions):
        for action in actions:
            self.native('action.settled', actionID=action['id'], fulfilled=True, origin=action['origin'])
            call = self.calls.get(action['binding']['conversationID'])
            if action['origin'] == 'muteEcho' or not call or action['kind'] == 'mute' and not action['handled']: continue
            call.pop('pending', None)
            if action['kind'] == 'end': self.calls.pop(call['id'])
            elif action['kind'] == 'hold': call['status'] = 'held'
            elif action['kind'] == 'mute': call['isMuted'] = action['params']['muted']
        self.snapshot()
        for binding in self.index['bindings']:
            if binding['conversationID'] in self.calls: self.hook(binding)
        if not self.calls: self.hook()

    def action(self, operation, binding):
        control = self.control(operation, callUUID=binding['callUUID'])
        actions = self.transaction([('join' if operation == 'answer' else 'end', binding, {})], control,
                                   signal=self.case != 'push-launch' or operation != 'answer')
        if self.case == 'push-launch' and operation == 'answer':
            call = self.calls[binding['conversationID']]
            call['status'] = 'connecting'
            call['pending'] = dict(id=self.uuid(), kind='join', origin='system', deadline=actions[0]['deadline'])
            self.snapshot()
            self.command('attach-handler', body=lambda command: (self.app('system.handlerAttached', attached=True), self.handler(actions[0])))
        self.settle(actions)

    def connected(self, binding):
        def report(command):
            self.app('system.backendConnected', id=command['id'], conversationID=binding['conversationID'])
            self.calls[binding['conversationID']].update(status='connected', hasConnectedAt=True)
            self.snapshot()
            self.hook(binding)
        self.command('backend-connected', binding, report)

    def build(self):
        first = self.push('first', cold=self.case == 'push-launch')
        if self.case == 'decline': self.action('decline', first)
        else:
            self.action('answer', first)
            self.connected(first)
            if self.case in ('hold-accept', 'end-accept'):
                waiting = self.push('waiting')
                operation = 'hold-and-accept' if self.case == 'hold-accept' else 'end-and-accept'
                control = self.control(operation, callUUID=waiting['callUUID'], activeCallUUID=first['callUUID'])
                bindings = dict(active=first, waiting=waiting)
                reverse = {value: key for key, value in gate.NATIVE.items()}
                specs = [(reverse[item['kind']], bindings[item['role']], {'held': item['onHold']} if 'onHold' in item else {})
                         for item in self.expected[operation]]
                self.settle(self.transaction(specs, control))
                self.connected(waiting)
                self.action('end', waiting)
                if self.case == 'hold-accept': self.action('end', first)
            elif self.case == 'mute-echo':
                control = self.control('mute', callUUID=first['callUUID'], muted=True)
                self.settle(self.transaction([('mute', first, {'muted': True})], control))
                self.push('first', duplicate=True)
                pending = []
                self.command('app-mute', first, lambda command: pending.extend(self.transaction([('mute', first, {'muted': False})], origin='application')))
                rejected = self.control('reset')
                rejected['response'].update(success=False, error='selected-provider reset requires no pending runtime actions')
                self.native('control.rejected', requestID=rejected['request']['id'], operation='reset', error=rejected['response']['error'])
                control = self.control('mute', callUUID=first['callUUID'], muted=False)
                self.settle(self.transaction([('mute', first, {'muted': False})], control, signal=False))
                self.command('release-mute', body=lambda command: self.settle(pending))
                self.settle(self.transaction([('mute', first, {'muted': False})], origin='muteEcho', signal=False))
                control = self.control('tones', callUUID=first['callUUID'], digits='12#A')
                self.settle(self.transaction([('tones', first, {'digits': '12#A'})], control))
                self.action('end', first)
            elif self.case == 'provider-reset':
                control = self.control('reset')
                control['response']['result'] = dict(reset=True, provider='synthetic-provider')
                self.native('provider.reset', requestID=control['request']['id'], callUUIDs=[first['callUUID']], cancelledActionIDs=[])
                self.app('system.event', name='providerReset')
                self.calls.clear()
                self.snapshot()
            else: self.action('end', first)
        self.hook()
        self.command('finish')
        self.app('sdk.result', success=True)
        self.app('done')
        return dict(rows=self.rows, engine=self.engine, index=self.index, controls=self.controls,
                    fixture_commands=self.commands, case=self.case, expected_d5=self.expected, launch_receipt=self.launch_receipt)


def first(values, event):
    return next(value for value in values if value['event'] == event)


def move_before(value, row, target):
    """Tamper causal order while retaining a well-formed monotone journal."""
    times = [event['t_ms'] for event in value['engine']]
    value['engine'].remove(row)
    value['engine'].insert(value['engine'].index(target), row)
    for sequence, (event, elapsed) in enumerate(zip(value['engine'], times), 1):
        event.update(sequence=sequence, t_ms=elapsed)
    for binding in value['index']['bindings']:
        binding['reportSequence'] = next(event['sequence'] for event in value['engine']
            if event['event'] == 'report.incoming' and event.get('pushID') == binding['pushID'])


def expired_settlement(value):
    ready = first(value['rows'], 'system.ready')
    marker = first(value['engine'], 'system.clock')
    deadline = first(value['engine'], 'action.deadline')
    settled = first(value['engine'], 'action.settled')
    elapsed = deadline['deadlineMS'] - ready['wallTimeMS'] + marker['t_ms'] - 1
    difference = elapsed - settled['t_ms']
    for event in value['engine'][value['engine'].index(settled):]: event['t_ms'] += difference


def duplicate_rows(value):
    identity = [control['request']['id'] for control in value['controls'] if control['request']['operation'] == 'push'][1]
    return [row for row in value['engine'] if row.get('pushID') == identity]


def legacy_pushes(value, required=True):
    for row in value['engine']:
        if row['event'] == 'push.received': row['api'] = 'legacy'
        if required and row['event'] in ('push.received', 'push.callbackReturned', 'push.completed'): row['mustReport'] = True


def old_m1_stays_app_only():
    with tempfile.TemporaryDirectory(prefix='sdk-system-legacy-origin-') as temporary:
        root = Path(temporary)
        legacy.fixture_case(root, 'journey', 12345)
        rows = legacy.gate.m0.load(root, 'sdk-m1-journey')
        engine = [json.loads(line) for line in (root / 'sdk-m1-journey.runtime.jsonl').read_text().splitlines()]
        deadlines = legacy.gate.deadline_oracles()[0]
        legacy.gate.case_conformance(rows, engine, 'journey', '27.0', deadlines)
        first(rows, 'm1.handler')['origin'] = 'system'
        try: legacy.gate.case_conformance(rows, engine, 'journey', '27.0', deadlines)
        except gate.EvidenceError as error:
            if 'invalid handler deadline/origin' not in str(error): raise
        else: raise RuntimeError('legacy M1 accepted a system-origin handler')


def manifest_controls():
    checks = []
    identity = dict(sourceCommit='a' * 40, sourceInputsSHA256='b' * 64)

    def write(path, value):
        path.write_text(json.dumps(value, indent=2) + '\n')

    def rehash(root):
        path = root / 'manifest.json'
        value = json.loads(path.read_text())
        value['files'] = {str(path.relative_to(root)): gate.m0.digest(path) for path in root.rglob('*')
                          if path.is_file() and path.name not in ('manifest.json', 'summary.json')}
        write(path, value)

    def change(path, **fields):
        value = json.loads(path.read_text()); value.update(fields); write(path, value)

    def owned_inventory(root, parent):
        owner = json.loads((parent / 'manifest.json').read_text())['simulatorID']
        write(root / 'cleanup-inventory.json', dict(devices={'synthetic': [dict(udid=owner, state='Shutdown')]}))
        change(root / 'cleanup-confirmation.json', inventorySHA256=gate.m0.digest(root / 'cleanup-inventory.json'))
        rehash(root)

    cases = [
        ('manifest-positive', None, None),
        ('manifest-file-hash-dirty', lambda root, parent: (root / 'evidence.json').write_text('changed\n'), 'missing/extra/changed'),
        ('manifest-unlisted-file', lambda root, parent: (root / 'extra.json').write_text('{}\n'), 'missing/extra/changed'),
        ('manifest-missing-file', lambda root, parent: (root / 'evidence.json').unlink(), 'missing/extra/changed'),
        ('manifest-rehashed-wrong-source', lambda root, parent: (change(root / 'manifest.json', sourceCommit='c' * 40), rehash(root)), 'unbound system manifest'),
        ('manifest-parent-bytes-dirty', lambda root, parent: change(parent / 'manifest.json', sourceCommit='c' * 40), 'unbound system manifest'),
        ('manifest-rehashed-live-device-directory', lambda root, parent: (change(root / 'cleanup-confirmation.json', deviceDirectoryAbsent=False), rehash(root)), 'independent cleanup confirmation missing'),
        ('manifest-rehashed-inventory-present', owned_inventory, 'owned simulator remains in cleanup inventory'),
        ('manifest-rehashed-wrong-cleanup-owner', lambda root, parent: (change(root / 'cleanup-confirmation.json', simulatorID=str(uuid.UUID(int=999)).upper()), rehash(root)), 'independent cleanup confirmation missing'),
    ]
    for name, edit, expected in cases:
        with tempfile.TemporaryDirectory(prefix='sdk-system-manifest-controls-') as temporary:
            parent = Path(temporary); root = parent / 'system'; root.mkdir()
            owner = dict(runID='synthetic-manifest-run', simulatorID=str(uuid.UUID(int=100)).upper())
            write(parent / 'manifest.json', dict(**identity, **owner))
            write(root / 'evidence.json', dict(fixtureKind='synthetic'))
            write(root / 'cleanup-inventory.json', dict(devices={'synthetic': []}))
            write(root / 'cleanup-confirmation.json', dict(simulatorID=owner['simulatorID'], inventoryAbsent=True,
                  deviceDirectoryAbsent=True, inventorySHA256=gate.m0.digest(root / 'cleanup-inventory.json')))
            write(root / 'manifest.json', dict(schemaVersion=1, scenario='sdk-system', **identity, **owner,
                  parentManifestSHA256=gate.m0.digest(parent / 'manifest.json')))
            rehash(root)
            if edit: edit(root, parent)
            try: gate.manifest_check(root, parent, identity)
            except gate.EvidenceError as error:
                if expected is None or expected not in str(error): raise RuntimeError(name + ': ' + str(error)) from error
            else:
                if expected is not None: raise RuntimeError(name + ': tampered filesystem evidence passed')
            checks.append(dict(name=name, passed=True, expected=expected or 'synthetic filesystem manifest accepted'))
    return checks


def container_cache_controls():
    import run_sdk_system as runner
    require = gate.require
    checks = []
    with tempfile.TemporaryDirectory(prefix='sdk-system-container-controls-') as temporary:
        root = Path(temporary).resolve()
        container = root / 'container'; container.mkdir()
        package = root / 'package'; package.mkdir()

        def check(name, supplied, response=None, failure=None):
            queries, journeys = [], []
            args = SimpleNamespace(package=package, output=root / name, simulator='synthetic-owned-simulator',
                                   run_id='synthetic-run', data_container=supplied)
            class StubJourney:
                def __init__(self, args, directory, case, selected):
                    journeys.append((case, selected))
                def run(self): pass
            def query(argv, **kwargs):
                queries.append(argv)
                if supplied is not None: raise AssertionError('Supplied container must never trigger another query')
                require(argv == ['xcrun', 'simctl', 'get_app_container', args.simulator, runner.BUNDLE, 'data'] and
                        kwargs == dict(capture_output=True, text=True, timeout=60), 'container query changed ownership or bounds')
                return response
            with patch.object(runner.m0, 'source_identity', return_value={}), \
                 patch.object(runner, 'Journey', StubJourney), patch.object(runner.subprocess, 'run', side_effect=query), \
                 patch.object(gate, 'package_check', return_value={}):
                try: runner.run(args)
                except gate.EvidenceError as error:
                    require(failure and failure in str(error), name + ': unexpected refusal ' + str(error))
                else:
                    require(failure is None, name + ': missing refusal')
            require(len(queries) == (0 if supplied is not None else 1), name + ': query was repeated or missing')
            require(journeys == ([] if failure else [(case, container) for case in runner.CASES]),
                    name + ': seven cases did not reuse the same validated path')
            checks.append(dict(name=name, passed=True, expected=failure or 'seven stub journeys share one validated container'))

        check('parent-container-no-query', container)
        check('fallback-container-one-query', None, SimpleNamespace(returncode=0, stdout=str(container) + '\n', stderr=''))
        check('invalid-parent-container-refused', root / 'missing', failure='missing or invalid')
        check('failed-container-query-refused', None, SimpleNamespace(returncode=1, stdout='', stderr='synthetic refusal'),
              'get_app_container failed')
        check('empty-container-query-refused', None, SimpleNamespace(returncode=0, stdout='', stderr=''), 'missing or invalid')
        check('invalid-container-query-refused', None, SimpleNamespace(returncode=0, stdout=str(root / 'missing') + '\n', stderr=''),
              'missing or invalid')
        # Execute only the exact parent dispatch block with a fake python3.
        # No simulator command or the full M0 lifecycle is executed.
        parent = (gate.ROOT / 'discovery/run_sdk_m0.sh').read_text()
        marker = 'if [[ "${SDK_SYSTEM_ACCEPTANCE:-0}" == 1 ]]; then\n  if [[ -n'
        require(parent.count(marker) == 1, 'parent system dispatch seam differs')
        start = parent.index(marker)
        block = parent[start:parent.index('\ncleanup_simulator', start)]
        binary = root / 'bin'; binary.mkdir()
        fake = binary / 'python3'
        fake.write_text('#!/bin/bash\nprintf \'%s\\0\' "$@" > "$ARGUMENT_CAPTURE"\n')
        fake.chmod(0o700)
        for supplied in (True, False):
            capture = root / ('parent-arguments-' + str(supplied))
            env = dict(os.environ, PATH=str(binary) + os.pathsep + os.defpath,
                       ARGUMENT_CAPTURE=str(capture), SDK_SYSTEM_ACCEPTANCE='1',
                       INHERITED_DYLD='/synthetic/inherited.dylib', UDID='synthetic-owned-simulator',
                       RUN_ID='synthetic-run', SDK_SYSTEM_CONTROL_PACKAGE=str(package), OUT=str(root / 'parent-output'))
            env.pop('example_container', None)
            if supplied: env['example_container'] = str(container)
            result = subprocess.run(['/bin/bash', '-eu', '-c', block], env=env,
                                    capture_output=True, text=True, timeout=5)
            require(result.returncode == 0 and not result.stdout and not result.stderr,
                    'parent container dispatch failed: ' + result.stderr)
            arguments = [part.decode() for part in capture.read_bytes().split(b'\0')[:-1]]
            expected = ['discovery/run_sdk_system.py', '--simulator', env['UDID'], '--run-id', env['RUN_ID'],
                        '--package', str(package), '--output', env['OUT'] + '/system']
            if supplied: expected += ['--data-container', str(container)]
            require(arguments == expected, 'parent dispatch lost cached container or changed fallback arguments')
            checks.append(dict(name='parent-shell-cached-path' if supplied else 'parent-shell-one-query-fallback',
                               passed=True, expected='exact parent Bash dispatch preserves optional cache arguments'))
    return checks


def cleanup_controls():
    """Exercise Journey's real finally; all processes, queries and signals are fake."""
    import run_sdk_system as runner
    checks = []
    for label in ('checkpoint-failure-graceful-reap', 'private-group-normal',
                  'timeout-kills-exact-owned-group', 'unknown-group-never-killed',
                  'owner-exited-no-group-kill', 'remaining-group-refuses'):
        with tempfile.TemporaryDirectory(prefix='sdk-system-cleanup-pure-') as temporary:
            root = Path(temporary)
            owner = 7001
            waits = []
            class Process:
                pid = owner
                alive = True
                def poll(self): return None if self.alive else 0
                def wait(self, timeout):
                    waits.append(timeout)
                    if timeout == 25 and label in ('timeout-kills-exact-owned-group', 'unknown-group-never-killed', 'owner-exited-no-group-kill'):
                        if label == 'owner-exited-no-group-kill': self.alive = False
                        raise subprocess.TimeoutExpired('synthetic-supervisor', timeout)
                    self.alive = False
                    return 0
                def terminate(self): raise AssertionError('PID-only supervisor termination bypasses launcher cleanup')
                def kill(self): raise AssertionError('PID-only supervisor kill bypasses launcher cleanup')
            process = Process()
            handlers = {sig: object() for sig in (signal.SIGTERM, signal.SIGINT)}
            previous = handlers.copy()
            def handler(sig, value):
                old = handlers[sig]; handlers[sig] = value; return old
            signals = []
            def group(group_id, value):
                assert group_id == owner and all(value is signal.SIG_IGN for value in handlers.values())
                signals.append((group_id, value))
                if value == 0:
                    if label == 'remaining-group-refuses': return
                    raise ProcessLookupError('synthetic absent group')
                assert value == signal.SIGKILL
                process.alive = False
            container = root / 'container'; container.mkdir()
            args = SimpleNamespace(run_id='synthetic-cleanup', simulator='synthetic-owned', package=root)
            journey = runner.Journey(args, root / 'case', 'decline', container)
            journey.runtime.parent.mkdir(parents=True)
            runner.write(journey.runtime.parent / 'session.json', dict(runID=args.run_id, pid=7003, bundleID=runner.BUNDLE))
            journey.runtime.mkdir()
            journey.journal.write_bytes(b'{"fixtureKind":"synthetic"}\n')
            receipt = journey.root / (journey.name + '.receipt.json')
            runner.write(receipt, dict(fixtureKind='synthetic'))
            def checkpoint(*_):
                if label not in ('private-group-normal', 'remaining-group-refuses'):
                    raise RuntimeError('synthetic checkpoint failure')
                return dict(pid=7003, sequence=1)
            journey.wait = checkpoint
            journey.push = Mock(return_value={}); journey.end = Mock(); journey.command = Mock()
            journey.sim = Mock(side_effect=RuntimeError('synthetic app-stop failure') if label == 'timeout-kills-exact-owned-group' else None)
            with patch.object(runner.subprocess, 'Popen', return_value=process) as spawn, \
                 patch.object(runner.signal, 'signal', side_effect=handler), \
                 patch.object(runner.os, 'getpgid', return_value=owner + (label == 'unknown-group-never-killed')), \
                 patch.object(runner.os, 'getsid', return_value=owner), patch.object(runner.os, 'killpg', side_effect=group):
                try: journey.run()
                except AssertionError: raise
                except Exception as error:
                    assert label != 'private-group-normal'
                    expected = 'ownership is unknown' if label == 'unknown-group-never-killed' else \
                        'group remains or cleanup is unknown' if label == 'remaining-group-refuses' else 'synthetic checkpoint failure'
                    assert expected in str(error), (label, str(error))
                else:
                    assert label == 'private-group-normal'
            assert handlers == previous
            assert spawn.call_args.kwargs.get('start_new_session') is True
            assert spawn.call_args.args[0].count('--inherit-launcher-group') == 1
            assert [item for item in signals if item[1] != 0] == (
                [(owner, signal.SIGKILL)] if label == 'timeout-kills-exact-owned-group' else [])
            assert waits == ([30] if label in ('private-group-normal', 'remaining-group-refuses') else
                             [25, 5] if label in ('timeout-kills-exact-owned-group', 'unknown-group-never-killed', 'owner-exited-no-group-kill') else [25])
            if label == 'timeout-kills-exact-owned-group':
                cleanup = json.loads((journey.root / 'failed-cleanup.json').read_text())
                assert cleanup['forcedGroupKill'] is True and 'app-stop failure' in cleanup['appStopError']
            if label == 'private-group-normal':
                value = json.loads(receipt.read_text())
                assert value['supervisorGroupAbsentAfterStop'] is True
                assert value['journalSHA256'] == gate.m0.digest(journey.root / (journey.name + '.runtime.jsonl'))
                assert json.loads((journey.root / 'index.json').read_text())['supervisorPID'] == owner
            elif label == 'remaining-group-refuses':
                assert 'supervisorGroupAbsentAfterStop' not in json.loads(receipt.read_text())
            else:
                journey.sim.assert_called_once_with('terminate', args.simulator, runner.BUNDLE, timeout=10)
            checks.append(dict(name=label, passed=True, expected='owned cleanup; never native acceptance'))
    return checks


def supervisor_controls():
    receipt = dict(inheritLauncherGroup=True, supervisorPID=7001, supervisorPGID=7001,
                   supervisorSID=7001, launcherPGID=7001, launcherPID=7002, appPID=7003,
                   supervisorGroupAbsentAfterStop=True)
    index = dict(supervisorPID=7001)
    checks = []
    def check(name, value, owner=index, cleanup=False, accepted=False):
        try: gate.supervisor_check(value, owner, cleanup)
        except gate.EvidenceError:
            assert not accepted, name
        else:
            assert accepted, name
        checks.append(dict(name=name, passed=True, expected='strict normal private-group binding'))
    check('normal-supervisor-binding', receipt, accepted=True)
    for key in ('inheritLauncherGroup', 'supervisorPID', 'supervisorPGID', 'supervisorSID',
                'launcherPGID', 'supervisorGroupAbsentAfterStop'):
        value = receipt.copy(); value.pop(key)
        check('missing-' + key, value)
    check('wrong-supervisor-owner', receipt, dict(supervisorPID=7999))
    check('boolean-supervisor-owner', receipt, dict(supervisorPID=True))
    check('wrong-launcher-group', dict(receipt, launcherPGID=7999))
    check('boolean-launcher-group', dict(receipt, launcherPGID=True))
    check('supervisor-app-collision', dict(receipt, appPID=7001))
    check('remaining-supervisor-group', dict(receipt, supervisorGroupAbsentAfterStop=False))
    check('failure-cleanup-cannot-qualify', receipt, cleanup=True)
    return checks


def controls():
    expected = gate.d5_compositions([json.loads(line) for line in (gate.ROOT / gate.D5).read_text().splitlines()])
    fixtures = {case: Fixture(case, expected).build() for case in gate.CASES}
    checks = []

    def check(name, case, edit=None, error=None, report_start_ms=None):
        value = (Fixture(case, expected, report_start_ms).build() if report_start_ms is not None
                 else deepcopy(fixtures[case]))
        if edit: edit(value)
        try:
            result = gate.validate_case(**value)
        except gate.EvidenceError as rejected:
            if error is None or error not in str(rejected):
                raise RuntimeError(name + ': unexpected rejection: ' + str(rejected)) from rejected
        else:
            if error is not None: raise RuntimeError(name + ': tampered evidence passed')
            if result.get('passed') is not True: raise RuntimeError(name + ': positive fixture failed')
        checks.append(dict(name=name, passed=True, expected=error or 'synthetic logical case accepted'))

    for case in gate.CASES: check('positive-' + case, case)
    for delay in (0, 499):
        check('cold-report-start-' + str(delay), 'push-launch', report_start_ms=delay)
    check('cold-report-start-at-budget', 'push-launch', error='cold PushKit report start exceeded 500 ms', report_start_ms=500)
    check('warm-report-start-outside-cold-budget', 'answer-end', report_start_ms=500)
    check('removed-shared-owner', 'answer-end', lambda value: first(value['rows'], 'sdk.managerCreated').update(owner='ConversationClient.shared'), 'SDK journey failed')
    check('reset-event-fabricated-conversation', 'provider-reset', lambda value: first(value['rows'], 'system.event').update(
        conversationID=value['index']['bindings'][0]['conversationID']), 'invalid global SDK event context')
    check('audio-event-fabricated-conversation', 'provider-reset', lambda value: first(value['rows'], 'system.event').update(
        name='audioActivated', conversationID=value['index']['bindings'][0]['conversationID']), 'invalid global SDK event context')
    check('positive-legacy-duplicate-required', 'mute-echo', legacy_pushes)
    check('system-handler-as-app', 'answer-end', lambda value: first(value['rows'], 'system.handler').update(origin='app'), 'handler origin/kind/role unbound')
    check('native-origin-as-app', 'answer-end', lambda value: first(value['engine'], 'transaction.delivered').update(origin='application'), 'system native action binding/origin differs')
    check('control-nonce-mismatch', 'answer-end', lambda value: value['controls'][1]['response'].update(id=str(uuid.UUID(int=999)).upper()), 'control identity differs')
    check('D5-order', 'hold-accept', lambda value: next(row for row in value['engine'] if row['event'] == 'transaction.delivered' and len(row['kinds']) == 2)['kinds'].reverse(), 'native composition/parameters differ')
    check('D5-wrong-target', 'end-accept', lambda value: next(row for row in value['engine'] if row['event'] == 'transaction.delivered' and len(row['kinds']) == 2)['callUUIDs'].reverse(), 'native composition/parameters differ')
    check('D5-hold-value', 'hold-accept', lambda value: next(row for row in value['engine'] if row['event'] == 'transaction.delivered' and len(row['kinds']) == 2)['parameters'][0].update(held=False), 'native composition/parameters differ')
    check('D5-held-projection', 'hold-accept', lambda value: [call.update(status='connected') for row in value['rows'] if row['event'] == 'sdk.snapshot' for call in row['conversations'] if call['status'] == 'held'], 'D5 projection did not compose')
    check('D5-held-hook', 'hold-accept', lambda value: [row.update(status='connected') for row in value['rows'] if row['event'] == 'system.hook' and row.get('status') == 'held'], 'held first-call hook missing')
    check('mute-projection-stays-false', 'mute-echo', lambda value: [call.update(isMuted=False) for row in value['rows'] if row['event'] == 'sdk.snapshot' for call in row['conversations']], 'mute projection/hook did not round trip')
    check('mute-hook-stays-false', 'mute-echo', lambda value: [row.update(muted=False) for row in value['rows'] if row['event'] == 'system.hook'], 'mute projection/hook did not round trip')
    check('decline-connected', 'decline', lambda value: next(row for row in value['rows'] if row['event'] == 'sdk.snapshot' and row['conversations'])['conversations'][0].update(status='connected'), 'declined call connected')
    check('native-action-id', 'answer-end', lambda value: value['controls'][1]['response']['result'].update(actionIDs=[str(uuid.UUID(int=999)).upper()]), 'system native action binding/origin differs')
    check('settlement-at-expiry', 'answer-end', expired_settlement, 'native settlement reached platform expiry')
    check('effective-margin-absent', 'answer-end', lambda value: first(value['rows'], 'system.handler').update(deadline=iso(first(value['engine'], 'action.deadline')['deadlineMS'])), 'handler deadline lacks effective safety margin')
    check('pending-effective-deadline', 'answer-end', lambda value: next(row for row in value['rows'] if row['event'] == 'sdk.snapshot' and row['conversations'] and row['conversations'][0].get('pending'))['conversations'][0]['pending'].update(deadline=iso(WALL)), 'handler/pending deadline mismatch')
    check('handler-marker-before-delivery', 'answer-end', lambda value: move_before(value, first(value['engine'], 'system.handlerEntered'), first(value['engine'], 'transaction.delivered')), 'handler/native causal order differs')
    check('handler-marker-deadline', 'answer-end', lambda value: first(value['engine'], 'system.handlerEntered').update(deadline=iso(WALL)), 'handler/native causal order differs')
    check('report-after-callback', 'answer-end', lambda value: move_before(value, first(value['engine'], 'push.callbackReturned'), first(value['engine'], 'report.incoming')), 'incoming wrapper attempt/completion differs')
    check('duplicate-payload-different', 'mute-echo', lambda value: [control for control in value['controls'] if control['request']['operation'] == 'push'][1]['request']['payload'].update(other='different-event'), 'duplicate push was not the same event')
    check('duplicate-report', 'mute-echo', lambda value: value['engine'].append(dict(first(value['engine'], 'report.incoming'))), 'require one report.incoming')
    check('duplicate-report-attempt-missing', 'mute-echo', lambda value: value['engine'].remove(first(duplicate_rows(value), 'report.incoming')), 'incoming report-attempt inventory differs')
    check('duplicate-report-wrong-error', 'mute-echo', lambda value: first(duplicate_rows(value), 'report.completed').update(error=0), 'incoming wrapper attempt/completion differs')
    check('duplicate-report-wrong-call', 'mute-echo', lambda value: [row.update(callUUID=str(uuid.UUID(int=999)).upper()) for row in duplicate_rows(value) if row['event'] in ('report.incoming', 'report.completed')], 'duplicate invitation targeted another call')
    check('duplicate-rings-again', 'mute-echo', lambda value: next(row for row in value['rows'] if row['event'] == 'sdk.snapshot' and row['conversations'] and row['conversations'][0].get('pending', {}).get('kind') == 'mute')['conversations'][0].update(status='ringing'), 'duplicate invitation rang again')
    check('legacy-optional-flag-cannot-suppress-report', 'mute-echo', lambda value: legacy_pushes(value, required=False), 'PushKit delivery/callback receipt differs')
    check('pending-echo-after-app-settlement', 'mute-echo', lambda value: move_before(value,
        next(row for row in value['engine'] if row['event'] == 'action.settled' and row['origin'] == 'application'),
        next(row for row in value['engine'] if row['event'] == 'transaction.delivered' and row['actionIDs'] ==
             next(control['response']['result']['actionIDs'] for control in value['controls'] if control['request']['operation'] == 'mute' and control['request']['muted'] is False))), 'same-value echo was not pending')
    check('pending-reset-wrong-rejection', 'mute-echo', lambda value: first(value['engine'], 'control.rejected').update(requestID=str(uuid.UUID(int=999)).upper()), 'pending reset refusal was not bound')
    check('reset-target', 'provider-reset', lambda value: first(value['engine'], 'provider.reset').update(callUUIDs=[]), 'selected-provider reset did not reach the SDK')
    check('reset-missing-event', 'provider-reset', lambda value: first(value['rows'], 'system.event').update(name='other'), 'unexpected SDK event context')
    check('cold-launched-active-app', 'push-launch', lambda value: value['launch_receipt'].update(appServiceAbsent=False), 'cold launch was not external/stopped/bound')
    active_services = SERVICES + '\n12345\t0\tUIKitApplication:' + gate.BUNDLE + '[synthetic-active]'
    check('cold-rehashed-active-service', 'push-launch', lambda value: value['launch_receipt'].update(
        serviceQuery=active_services, serviceQuerySHA256=hashlib.sha256(active_services.encode()).hexdigest()), 'cold launch app was still running')
    for label, services in (('empty', ''), ('malformed', 'PID\tStatus\tLabel\ninvalid\t0\tlabel\n')):
        check('cold-rehashed-' + label + '-service-table', 'push-launch', lambda value, services=services: value['launch_receipt'].update(
            serviceQuery=services, serviceQuerySHA256=hashlib.sha256(services.encode()).hexdigest()), 'cold launch app was still running')
    check('cold-envelope-hash', 'push-launch', lambda value: first(value['engine'], 'push.launchConsumed').update(envelopeSHA256='0' * 64), 'runtime consumed different launch bytes')
    check('cold-envelope-payload-unhashed', 'push-launch', lambda value: value['launch_receipt']['envelope']['payload'].update(tampered=True), 'cold launch envelope hash differs')
    check('cold-envelope-expiry', 'push-launch', lambda value: value['launch_receipt']['envelope'].update(expiresAtMS=WALL + 30001), 'cold envelope identity/lifetime differs')
    check('cold-pending-as-app', 'push-launch', lambda value: next(row for row in value['rows'] if row['event'] == 'sdk.snapshot' and row['conversations'] and row['conversations'][0].get('pending'))['conversations'][0]['pending'].update(origin='app'), 'cold system answer did not pend without handler')
    check('cold-completion-report-missing', 'push-launch', lambda value: first(value['engine'], 'push.completed').update(reportEntered=False), 'cold PushKit report/callback/completion missing')
    check('cold-incoming-completion-error', 'push-launch', lambda value: first(value['engine'], 'report.completed').update(error=2), 'cold PushKit report/callback/completion missing')
    check('fabricated-connected-before-backend', 'answer-end', lambda value: next(row for row in value['rows'] if row['event'] == 'sdk.snapshot' and row['conversations'] and row['conversations'][0]['status'] == 'connecting' and 'pending' not in row['conversations'][0])['conversations'][0].update(status='connected', hasConnectedAt=True), 'connected state was fabricated')
    check('final-hook-nonempty', 'answer-end', lambda value: [row for row in value['rows'] if row['event'] == 'system.hook'][-1].update(count=1), 'final hook not observed')
    old_m1_stays_app_only()
    checks.append(dict(name='legacy-M1-remains-app-only', passed=True, expected='system-origin legacy handler rejected'))
    gate.cold_push_reference()
    checks.append(dict(name='cold-device-reference', passed=True, expected='entered iOS27 report reference consumed'))
    checks.extend(manifest_controls())
    checks.extend(container_cache_controls())
    checks.extend(cleanup_controls())
    checks.extend(supervisor_controls())
    checks.extend(runner_environment_controls())
    return checks


def runner_environment_controls():
    import run_sdk_system as runner

    class LaunchBoundary(Exception):
        pass

    launches = []
    def launch(_command, **options):
        launches.append(options['env'])
        raise LaunchBoundary

    with tempfile.TemporaryDirectory(prefix='sdk-system-launch-controls-') as temporary:
        journey = runner.Journey.__new__(runner.Journey)
        journey.args = SimpleNamespace(run_id='synthetic-system-run', simulator='synthetic-simulator')
        journey.root = Path(temporary)
        journey.case = 'answer-end'
        journey.name = 'sdk-system-answer-end'
        journey.trace = journey.root / 'trace.log'
        with patch.dict(os.environ, {'SIMCTL_CHILD_SDK_M3_ACCEPTANCE': '1',
              'SIMCTL_CHILD_SDK_M3_RUN_ID': 'stale-direct-run',
              'SIMCTL_CHILD_SDK_M3_DELIVERY': 'direct-url-and-intent',
              'SIMCTL_CHILD_SDK_M2_RUN_ID': 'stale-media-run',
              'SIMCTL_CHILD_SDK_M2_CASE': 'stale-audio',
              'SIMCTL_CHILD_DYLD_INSERT_LIBRARIES': '/synthetic/inherited.dylib',
              'SDK_SYSTEM_UNRELATED_CONTROL': 'preserved'}), \
             patch.object(runner.subprocess, 'Popen', side_effect=launch), \
             patch.object(runner.subprocess, 'run', side_effect=AssertionError('native command forbidden')):
            try:
                journey.run()
            except LaunchBoundary:
                pass
        require = gate.require
        require(len(launches) == 1, 'system launch boundary not reached exactly once')
        env = launches[0]
        require(not any(key.startswith('SIMCTL_CHILD_SDK_M3_') for key in env),
                'system launch inherited M3 mode or run identity')
        require('SIMCTL_CHILD_SDK_M2_RUN_ID' not in env, 'system launch inherited M2 run identity')
        require(env['SIMCTL_CHILD_SDK_SYSTEM_RUN_ID'] == 'synthetic-system-run' and
                env['SIMCTL_CHILD_CKSIM_RUN_ID'] == 'synthetic-system-run' and
                env['SIMCTL_CHILD_SDK_SYSTEM_CASE'] == 'answer-end', 'system launch identity differs')
        require(env['SIMCTL_CHILD_SDK_M0_ACCEPTANCE'] == '0' and env['SIMCTL_CHILD_SDK_M1_CASE'] == '' and
                env['SIMCTL_CHILD_SDK_M2_CASE'] == '', 'system launch enabled another journey')
        require(env['SIMCTL_CHILD_DYLD_INSERT_LIBRARIES'] == '/synthetic/inherited.dylib' and
                env['SDK_SYSTEM_UNRELATED_CONTROL'] == 'preserved', 'system launch changed unrelated environment')
    return [dict(name='system-runner-isolates-M3-child-environment', passed=True,
                 expected='actual launcher environment isolated; subprocess boundary mocked')]


if __name__ == '__main__':
    print(json.dumps(dict(success=True, measurement='measured synthetic logical-gate and mocked launch-boundary controls only', checks=controls()), indent=2))
