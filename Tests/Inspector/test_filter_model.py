"""Exercise the actual InspectorModel HTTP submission and journal acknowledgement."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[2]
HARNESS = r'''
import Foundation
@main @MainActor struct FilterModelTest {
  static func wait(_ condition: () -> Bool) async throws {
    let deadline = Date().addingTimeInterval(5)
    while !condition() {
      precondition(Date() < deadline, "Model did not reach the required state")
      try await Task.sleep(for: .milliseconds(10))
    }
  }
  static func main() async throws {
    let mode = CommandLine.arguments[2]
    let model = InspectorModel(environment: ["CKSIM_INSPECTOR_URL": CommandLine.arguments[1],
                                           "CKSIM_INSPECTOR_TOKEN": "test-token"])
    let following = Task { await model.follow() }
    defer { following.cancel() }
    try await wait { model.online }
    let reasons: [InspectorIncomingFilter] = mode == "success" ? [.doNotDisturb, .none] : [.doNotDisturb]
    var prior = InspectorIncomingFilter.none
    for (offset, reason) in reasons.enumerated() {
      let id = "request-\(offset + 1)"
      let started = Date()
      model.configureIncomingFilter(reason)
      precondition(model.busy)
      try await wait { model.timeline.contains { $0.event == "test.commandAccepted" && $0.requestID == id } }
      precondition(model.busy && model.incomingFilter == prior && model.commandError == nil,
                   "HTTP acceptance must not optimistically change the selected filter")
      FileHandle.standardOutput.write(Data("release:\(offset + 1)\n".utf8))
      if mode == "wrong" {
        try await wait { model.timeline.contains { $0.event == "incomingFilter.configured" } }
        precondition(model.incomingFilter == reason && model.busy,
                     "An external journal event updates observed state without acknowledging this command")
      }
      try await wait { !model.busy }
      if mode == "success" {
        precondition(model.incomingFilter == reason && model.commandError == nil)
      } else {
        precondition(Date().timeIntervalSince(started) >= 2.9, "An unrelated or absent journal event cannot acknowledge this request")
        precondition(model.commandError?.contains("journal confirmation has not arrived") == true)
        precondition(model.incomingFilter == (mode == "wrong" ? reason : prior))
      }
      prior = reason
    }
  }
}
'''


class FilterModelTests(unittest.TestCase):
    def test_http_submission_waits_for_its_journal_request_id(self):
        with tempfile.TemporaryDirectory(prefix='inspector-filter-model-') as directory:
            work = Path(directory)
            source = (ROOT / 'Inspector/InspectorModel.swift').read_text().splitlines()
            self.assertEqual(source[0], '#if targetEnvironment(simulator)')
            self.assertEqual(source[-1], '#endif')
            model, harness, binary = work / 'InspectorModel.swift', work / 'Main.swift', work / 'model-test'
            model.write_text('\n'.join(source[1:-1]) + '\n')
            harness.write_text(HARNESS)
            compiled = subprocess.run(['swiftc', '-swift-version', '6', '-parse-as-library', str(model),
                *(str(ROOT / 'Inspector' / name) for name in ('RuntimeEvent.swift', 'RuntimeTimingReferences.swift', 'IncomingFilter.swift', 'MultiCallProjection.swift')),
                str(harness), '-o', str(binary)], text=True, capture_output=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            for mode in ('success', 'wrong', 'missing'):
                with self.subTest(mode=mode):
                    self.exercise(binary, mode)

    def exercise(self, binary, mode):
        session = dict(schemaVersion=1, bundleID='fixture', runID='host-test', pid=123, profile='ios-27.0')
        rows, commands, failures, output = [], [], [], []
        lock = threading.Lock()
        def append(event, **fields):
            rows.append(dict(session, sequence=len(rows) + 1, t_ms=(len(rows) + 1) * 10, event=event, **fields))
        def state(reason):
            return dict(reason=reason, code=0 if reason == 'none' else 3,
                        verification='not-applicable' if reason == 'none' else 'verified')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, value):
                if self.headers.get('Authorization') != 'Bearer test-token':
                    failures.append('Missing bearer token')
                payload = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path != '/events':
                    failures.append('Unexpected GET ' + self.path)
                after = int(parse_qs(parsed.query)['after'][0])
                with lock:
                    value = dict(session=session, events=rows[after:], cursor=len(rows))
                self.respond(value)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path != '/command' or self.headers.get('Content-Type') != 'application/json':
                    failures.append('Unexpected command route or encoding')
                with lock:
                    commands.append(body)
                    identifier = 'request-' + str(len(commands))
                    append('test.commandAccepted', requestID=identifier)
                self.respond(dict(success=True, id=identifier, result=dict(incomingFilter=state(body['reason']))))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        process = subprocess.Popen([str(binary), f'http://127.0.0.1:{server.server_port}', mode],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        def read_output():
            for line in process.stdout:
                output.append(line)
                if line.startswith('release:') and mode != 'missing':
                    index = int(line.split(':')[1])
                    with lock:
                        identifier = 'unrelated-request' if mode == 'wrong' else 'request-' + str(index)
                        append('incomingFilter.configured', requestID=identifier, **state(commands[index - 1]['reason']))
        reading = threading.Thread(target=read_output, daemon=True)
        reading.start()
        try:
            process.wait(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reading.join(timeout=2)
            process.stdout.close()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
        self.assertEqual(process.returncode, 0, ''.join(output))
        self.assertEqual(failures, [])
        reasons = ['do-not-disturb', 'none'] if mode == 'success' else ['do-not-disturb']
        self.assertEqual(commands, [dict(operation='incoming-filter', reason=reason) for reason in reasons])


if __name__ == '__main__':
    unittest.main()
