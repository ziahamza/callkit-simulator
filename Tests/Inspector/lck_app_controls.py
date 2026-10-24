#!/usr/bin/env python3
"""Actual InspectorModel HTTP -> shipping Tools bridge -> genuine LCK acceptance.

Only the HTTP listening fixture is test code: event batches, control parsing,
mailbox requests, callbacks and state snapshots all come from shipping code.
"""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import runpy
import secrets
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Tests/RuntimeEvents'))
from lck_acceptance import Journey, simulator_applications, simulator_scope
from run import run
from ci_simulator import simulator_uuid

MODEL_HARNESS = r'''
import Foundation
@main @MainActor struct NativeInspectorControls {
  static func wait(_ model: InspectorModel, _ label: String, _ condition: () -> Bool) async throws {
    let deadline = Date().addingTimeInterval(8)
    while !condition() {
      precondition(model.commandError == nil, model.commandError ?? "")
      precondition(Date() < deadline, "Inspector did not reach " + label)
      try await Task.sleep(for: .milliseconds(10))
    }
  }
  static func submit(_ model: InspectorModel, _ label: String, _ action: () -> Void) async throws {
    precondition(model.online && !model.busy && model.pending.isEmpty)
    action()
    precondition(model.busy, "Inspector rejected " + label + " before HTTP submission")
    try await wait(model, label) { !model.busy && model.pending.isEmpty }
    precondition(model.commandError == nil)
    print("Inspector model completed: " + label)
  }
  static func main() async throws {
    let model = InspectorModel(environment: ProcessInfo.processInfo.environment)
    let following = Task { await model.follow() }
    defer { following.cancel() }
    let a = CommandLine.arguments[1], b = CommandLine.arguments[2]
    try await wait(model, "two native calls") {
      model.online && model.conversations[a]?.held == true && model.conversations[b]?.held == false
    }
    // Ordinary Inspector controls retain system origin; this is the old app-UI
    // shape and must not generate End B when resuming held A.
    try await submit(model, "system Resume A") { model.send("hold", call: model.conversations[a]!, held: false) }
    try await wait(model, "both system-controlled calls joined") {
      model.conversations[a]?.held == false && model.conversations[b]?.ended != true
    }
    try await Task.sleep(for: .milliseconds(250))
    precondition(model.conversations[b]?.ended != true, "System Resume unexpectedly ended B")
    try await submit(model, "system Pause A") { model.send("hold", call: model.conversations[a]!, held: true) }
    try await wait(model, "A held again") { model.conversations[a]?.held == true }
    try await submit(model, "App Merge B into A") {
      model.send("merge", call: model.conversations[b]!, groupTarget: model.conversations[a]!, applicationRequest: true)
    }
    precondition(model.conversations[a]?.held == true && model.conversations[b]?.held == false)
    try await submit(model, "App Unmerge B") { model.send("unmerge", call: model.conversations[b]!, applicationRequest: true) }
    precondition(model.conversations[a]?.held == true && model.conversations[b]?.held == false)
    try await submit(model, "App Pause B") { model.send("hold", call: model.conversations[b]!, held: true, applicationRequest: true) }
    try await wait(model, "B held") { model.conversations[b]?.held == true }
    try await submit(model, "App Resume B") { model.send("hold", call: model.conversations[b]!, held: false, applicationRequest: true) }
    try await wait(model, "B active again") { model.conversations[b]?.held == false }
    try await submit(model, "App Resume A") { model.send("hold", call: model.conversations[a]!, held: false, applicationRequest: true) }
    try await wait(model, "App Resume ends B") {
      model.conversations[a]?.connected == true && model.conversations[a]?.held == false && model.conversations[b]?.ended == true
    }
    print("Inspector observed A joined / B left")
  }
}
'''


def process_with_log(arguments, log, **kwargs):
    def capture(stdout, stderr):
        def decoded(value):
            return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
        log.write_text(decoded(stdout) + decoded(stderr))
    try:
        result = subprocess.run(arguments, text=True, capture_output=True, **kwargs)
    except subprocess.TimeoutExpired as error:
        capture(error.stdout, error.stderr)
        raise
    capture(result.stdout, result.stderr)
    return result


def compile_model(work, out):
    source = (ROOT / 'Inspector/InspectorModel.swift').read_text().splitlines()
    assert (source[0], source[-1]) == ('#if targetEnvironment(simulator)', '#endif')
    model, harness, binary = work / 'InspectorModel.swift', work / 'Main.swift', work / 'native-inspector-model'
    model.write_text('\n'.join(source[1:-1]) + '\n')
    harness.write_text(MODEL_HARNESS)
    dependencies = [path for path in sorted((ROOT / 'Inspector').glob('*.swift'))
                    if path.name not in ('InspectorApp.swift', 'InspectorModel.swift')]
    built = process_with_log(['swiftc', '-swift-version', '6', '-parse-as-library', '-module-cache-path',
                              str(work / 'module-cache'), str(model), *map(str, dependencies), str(harness), '-o', str(binary)],
                             out / 'model-build.log', timeout=90)
    assert built.returncode == 0, built.stdout + built.stderr
    return binary


def verify_controls(commands, rows, a, b):
    expected = [dict(operation='hold', callUUID=a, held='false'),
                dict(operation='hold', callUUID=a, held='true'),
                dict(operation='merge', callUUID=b, groupWithCallUUID=a, applicationRequest='true'),
                dict(operation='unmerge', callUUID=b, applicationRequest='true'),
                dict(operation='hold', callUUID=b, held='true', applicationRequest='true'),
                dict(operation='hold', callUUID=b, held='false', applicationRequest='true'),
                dict(operation='hold', callUUID=a, held='false', applicationRequest='true')]
    assert [entry['body'] for entry in commands] == expected, 'Actual InspectorModel HTTP bodies differ from UI control contract'
    for index, command in enumerate(commands):
        receipt = json.loads(Path(command['receipt']).read_text())
        app = index >= 2
        request, response = receipt['request'], receipt['response']
        assert request.get('applicationRequest') is True if app else 'applicationRequest' not in request
        assert response['success'] is True
        ids = set(response['result']['actionIDs'])
        assert len(ids) == 1
        deadlines = [r for r in rows if r['event'] == 'action.deadline' and r.get('actionID') in ids]
        settled = [r for r in rows if r['event'] == 'action.settled' and r.get('actionID') in ids]
        native = [r for r in rows if r['event'] == 'lck.fixture.perform' and r.get('actionID') in ids]
        assert len(deadlines) == len(settled) == len(native) == 1
        origin = 'application' if app else 'system'
        assert deadlines[0]['origin'] == settled[0]['origin'] == origin, 'Inspector app request delivered with wrong native origin'
        assert deadlines[0]['apiKind'] == 'livecommunicationkit' and settled[0]['fulfilled'] is True
        kind = 'Merge' if index == 2 else 'Unmerge' if index == 3 else 'Pause'
        assert native[0]['kind'] == kind and native[0]['callUUID'] == request['callUUID']
        if index not in (2, 3):
            assert native[0]['held'] is (command['body']['held'] == 'true')
    resume_ids = set(json.loads(Path(commands[-1]['receipt']).read_text())['response']['result']['actionIDs'])
    resume = next(r for r in rows if r['event'] == 'action.settled' and r.get('actionID') in resume_ids)
    end_b = [r for r in rows if r['event'] == 'lck.fixture.perform' and r['kind'] == 'End' and r['callUUID'] == b]
    assert len(end_b) == 1 and end_b[0]['sequence'] > resume['sequence'], 'App Resume must generate End B after settlement'
    end_runtime = next(r for r in rows if r['event'] == 'action.deadline' and r.get('actionID') == end_b[0]['actionID'])
    assert end_runtime['origin'] == 'system', 'Generated End B must remain a system action'
    return dict(controls=len(commands),applicationControls=5,systemControls=2,
                endBAfterAppResumeMS=end_b[0]['t_ms']-resume['t_ms'])


def run_case(journey, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='inspector-native-model-', dir=out) as directory:
        binary = compile_model(Path(directory), out)
        journey.pair('inspectorApplicationControls', caps='none')
        journey.composite()
        journey.state(A='paused', B='joined')
        cli = runpy.run_path(str(ROOT / 'Tools/cksim'))
        arguments = SimpleNamespace(bundle=journey.session['bundleID'], app_container=journey.container,
                                    runtime_dir=ROOT, timeout=5, command_receipt=None)
        bridge = cli['InspectorBridge'](arguments, journey.device, journey.channel)
        commands, receipt_paths = [], []
        lock = threading.Lock()
        token = secrets.token_urlsafe(32)
        actual_control = cli['control']
        def recorded_control(args, device, expected_session=None):
            with lock:
                receipt = out / f'mailbox-control-{len(receipt_paths)+1}.json'
                receipt_paths.append(receipt)
            return actual_control(SimpleNamespace(**dict(vars(args), command_receipt=receipt)), device,
                                  expected_session=expected_session)
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)
            def log_message(self, *parts): pass
            def respond(self, value, status=200):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def authorized(self):
                return hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)
            def do_GET(self):
                try:
                    parsed = urlsplit(self.path)
                    assert self.authorized() and parsed.path == '/events'
                    self.respond(bridge.events(int(parse_qs(parsed.query)['after'][0])))
                except Exception as error:
                    self.respond(dict(error=str(error)), 409)
            def do_POST(self):
                try:
                    assert self.authorized() and self.path == '/command' and self.headers.get('Content-Type') == 'application/json'
                    size = int(self.headers['Content-Length'])
                    assert 0 < size <= 4096
                    body = json.loads(self.rfile.read(size))
                    with lock:
                        entry = dict(body=body)
                        commands.append(entry)
                    (out / 'actual-model-http.json').write_text(json.dumps(commands, indent=2))
                    result = bridge.command(body)
                    entry['receipt'] = str(receipt_paths[-1])
                    (out / 'actual-model-http.json').write_text(json.dumps(commands, indent=2))
                    self.respond(result)
                except Exception as error:
                    self.respond(dict(error=str(error)), 409)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            with patch.dict(bridge.command.__globals__, control=recorded_control):
                result = process_with_log([str(binary), journey.ids['A'], journey.ids['B']], out / 'model.log',
                                          env=dict(os.environ, CKSIM_INSPECTOR_URL=f'http://127.0.0.1:{server.server_port}',
                                                   CKSIM_INSPECTOR_TOKEN=token), timeout=45)
            print(result.stdout, end='', flush=True)
            data = journey.rows()
            summary = verify_controls(commands, data, journey.ids['A'], journey.ids['B'])
            assert result.returncode == 0, result.stdout + result.stderr
            journey.state(A='joined', B='left')
            journey.save()
            (out / 'summary.json').write_text(json.dumps(summary, indent=2))
            print(json.dumps(dict(success=True, driver='actual InspectorModel HTTP + shipping Tools bridge', **summary)), flush=True)
            return summary
        finally:
            server.shutdown(); server.server_close(); serving.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--simulator', type=simulator_uuid, help='Borrow an already booted operator-owned simulator')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with simulator_scope(args.simulator, args.out) as (device, owned):
        journey = None
        try:
            if owned:
                run('xcrun', 'simctl', 'boot', device, timeout=60)
                run('xcrun', 'simctl', 'bootstatus', device, '-b', timeout=300)
            applications = simulator_applications(device)
            run('xcrun', 'simctl', 'install', device, args.app.resolve(), timeout=120)
            journey = Journey(device, None, args.out, applications=applications)
            run_case(journey, args.out / 'inspector-model-controls')
        finally:
            if journey is not None:
                journey.save()


if __name__ == '__main__':
    main()
