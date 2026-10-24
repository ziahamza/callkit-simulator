"""Exercise actual InspectorModel LCK submissions without simulator or policy fakes."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[2]
ACTIVE, HELD, WAITING = [f'{index:08X}-1111-4111-8111-111111111111' for index in (1, 2, 3)]
HARNESS = r'''
import Foundation
@main @MainActor struct LCKModelTest {
  static func wait(_ condition: () -> Bool) async throws {
    let limit = Date().addingTimeInterval(5)
    while !condition() {
      precondition(Date() < limit, "Model did not consume the expected journal state")
      try await Task.sleep(for: .milliseconds(10))
    }
  }
  static func emit(_ value: String) { FileHandle.standardOutput.write(Data((value + "\n").utf8)) }
  static func states(_ model: InspectorModel) -> [String] {
    model.conversations.values.map { "\($0.uuid!):\($0.status)" }.sorted()
  }
  static func main() async throws {
    let model = InspectorModel(environment: ["CKSIM_INSPECTOR_URL": CommandLine.arguments[1], "CKSIM_INSPECTOR_TOKEN": "lck-token"])
    let following = Task { await model.follow() }
    defer { following.cancel() }
    let activeID = "00000001-1111-4111-8111-111111111111"
    let heldID = "00000002-1111-4111-8111-111111111111"
    let waitingID = "00000003-1111-4111-8111-111111111111"
    try await wait { model.online && model.conversations.count == 3 }
    let configuration = model.providerConfigurations.first { $0.provider == "p1" }
    precondition(configuration?.apiKind == "livecommunicationkit" && configuration?.maximumCallGroups == 2 && configuration?.maximumCallsPerCallGroup == 1)
    precondition(model.conversations[waitingID]?.ringing == true && model.conversations[waitingID]?.capabilities == [] &&
                 model.conversations[waitingID]?.availableSystemChoices == ["end-held-and-accept"])
    for index in 1...5 {
      let active = model.conversations[activeID]!
      let held = model.conversations[heldID]!
      let waiting = model.conversations[waitingID]!
      let prior = states(model)
      switch index {
      case 1: model.send("hold", call: active, held: true, applicationRequest: true)
      case 2: model.send("hold", call: active, held: false, applicationRequest: true)
      case 3: model.send("merge", call: active, groupTarget: waiting, applicationRequest: true)
      case 4: model.send("unmerge", call: active, applicationRequest: true)
      default: model.send("end-held-and-accept", call: waiting, active: active, heldCall: held)
      }
      precondition(model.busy)
      try await wait { model.timeline.contains { $0.event == "test.commandAccepted" && $0.requestID == "command-\(index)" } }
      precondition(model.busy && states(model) == prior, "HTTP acceptance cannot optimistically alter call state")
      emit("deliver:\(index)")
      try await wait { !model.busy && !model.pending.isEmpty }
      precondition(model.commandError == nil)
      if index == 3 { precondition(model.isPending(waiting), "Ringing merge target must be pending despite empty capabilities") }
      if index == 5 { precondition(model.isPending(active) && model.isPending(held) && model.isPending(waiting)) }
      emit("settle:\(index)")
      try await wait { model.pending.isEmpty }
      if index <= 2 { precondition(model.conversations[activeID]?.held == (index == 1)) }
    }
    precondition(model.conversations[activeID]?.held == true && model.conversations[heldID]?.ended == true)
    precondition(model.conversations[waitingID]?.connected == true && model.incoming == nil)
    emit("overflow")
    try await wait { model.timeline.contains { $0.event == "action.settled" && $0.actionID == "overflow-first" } }
    precondition(model.pending["overflow-first"] == nil && model.pending["overflow-second"] != nil)
    precondition(model.isPending(model.conversations[activeID]!) && model.isPending(model.conversations[waitingID]!),
                 "A new transaction must survive cache overflow before its deadlines arrive")
  }
}
'''


class LCKModelTests(unittest.TestCase):
    def test_pause_merge_unmerge_and_end_held_accept_use_public_http(self):
        with tempfile.TemporaryDirectory(prefix='inspector-lck-model-') as directory:
            work = Path(directory)
            source = (ROOT / 'Inspector/InspectorModel.swift').read_text().splitlines()
            self.assertEqual((source[0], source[-1]), ('#if targetEnvironment(simulator)', '#endif'))
            model, harness, binary = work / 'InspectorModel.swift', work / 'Main.swift', work / 'model-test'
            model.write_text('\n'.join(source[1:-1]) + '\n')
            harness.write_text(HARNESS)
            dependencies = [path for path in sorted((ROOT / 'Inspector').glob('*.swift')) if path.name not in ('InspectorApp.swift', 'InspectorModel.swift')]
            compiled = subprocess.run(['swiftc', '-swift-version', '6', '-parse-as-library', str(model),
                                      *map(str, dependencies), str(harness), '-o', str(binary)],
                                     text=True, capture_output=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            self.exercise(binary)

    def exercise(self, binary):
        session = dict(schemaVersion=1, bundleID='fixture', runID='host-lck', pid=123, profile='ios-27.0')
        rows, commands, failures, output = [], [], [], []
        lock = threading.Lock()
        calls = {identifier: dict(uuid=identifier, connected=identifier != WAITING, ringing=identifier == WAITING,
                                 ended=False, held=identifier == HELD, capabilities=[], apiKind='livecommunicationkit',
                                 availableSystemChoices=['end-held-and-accept'] if identifier == WAITING else [])
                 for identifier in (ACTIVE, HELD, WAITING)}
        def append(event, **fields):
            rows.append(dict(session, sequence=len(rows) + 1, t_ms=(len(rows) + 1) * 10, event=event, **fields))
        append('provider.created', provider='p1', maximumCallGroups=2, maximumCallsPerCallGroup=1)
        append('provider.apiKind', provider='p1', apiKind='livecommunicationkit')
        for call in calls.values(): append('call.changed', **call)
        def actions(index):
            definitions = [(ACTIVE, 'hold', {'held': True}), (HELD, 'end', {}), (WAITING, 'answer', {})] if index == 5 else [
                (ACTIVE, 'hold' if index <= 2 else 'group', {'held': index == 1} if index <= 2 else {'groupTargetUUID': WAITING if index == 3 else ''})]
            return [dict(actionID=f'action-{index}-{offset}', callUUID=call, kind=kind, transactionID=f'transaction-{index}',
                         origin='system', provider='p1', apiKind='livecommunicationkit', **parameters)
                    for offset, (call, kind, parameters) in enumerate(definitions)]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def respond(self, value):
                if self.headers.get('Authorization') != 'Bearer lck-token': failures.append('Missing bearer token')
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path != '/events': failures.append('Unexpected GET ' + self.path)
                after = int(parse_qs(parsed.query)['after'][0])
                with lock: value = dict(session=session, events=rows[after:], cursor=len(rows))
                self.respond(value)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path != '/command' or self.headers.get('Content-Type') != 'application/json': failures.append('Unexpected command route or content type')
                with lock:
                    commands.append(body)
                    index = len(commands)
                    append('test.commandAccepted', requestID=f'command-{index}')
                self.respond(dict(success=True, result=dict(actionIDs=[row['actionID'] for row in actions(index)], transactionID=f'transaction-{index}')))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        process = subprocess.Popen([str(binary), f'http://127.0.0.1:{server.server_port}'], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        def read_output():
            for line in process.stdout:
                output.append(line)
                if line.strip() == 'overflow':
                    with lock:
                        for number in range(100):
                            identifier = f'{number + 100:08X}-1111-4111-8111-111111111111'
                            append('transaction.delivered', transactionID=f'old-{number}', actionIDs=[f'old-action-{number}'], callUUIDs=[identifier])
                            append('action.deadline', actionID=f'old-action-{number}', callUUID=identifier, transactionID=f'old-{number}')
                        append('transaction.delivered', transactionID='overflow-new', actionIDs=['overflow-first', 'overflow-second'], callUUIDs=[ACTIVE, WAITING])
                        append('action.deadline', actionID='overflow-first', callUUID=ACTIVE, transactionID='overflow-new')
                        append('action.deadline', actionID='overflow-second', callUUID=WAITING, transactionID='overflow-new')
                        append('action.settled', actionID='overflow-first', callUUID=ACTIVE, transactionID='overflow-new', fulfilled=True)
                    continue
                if not line.startswith(('deliver:', 'settle:')): continue
                stage, number = line.strip().split(':')
                index = int(number)
                with lock:
                    if stage == 'deliver':
                        constituents = actions(index)
                        classNames = {'hold': 'CXSetHeldCallAction', 'end': 'CXEndCallAction',
                                      'answer': 'CXAnswerCallAction', 'group': 'CXSetGroupCallAction'}
                        append('transaction.delivered', transactionID=f'transaction-{index}', provider='p1',
                               origin='system',
                               actionIDs=[row['actionID'] for row in constituents],
                               callUUIDs=[row['callUUID'] for row in constituents],
                               kinds=[classNames[row['kind']] for row in constituents],
                               parameters=[{key: row[key] for key in ('held', 'groupTargetUUID') if key in row}
                                           for row in constituents])
                        for row in constituents: append('action.deadline', **row)
                    else:
                        if index <= 2: calls[ACTIVE]['held'] = index == 1
                        if index == 5:
                            calls[HELD]['ended'] = True
                            calls[ACTIVE]['held'] = True
                            calls[WAITING].update(connected=True, ringing=False, availableSystemChoices=[])
                        for call in calls.values(): append('call.changed', **call)
                        for row in actions(index): append('action.settled', fulfilled=True, **row)
        reading = threading.Thread(target=read_output, daemon=True)
        reading.start()
        try:
            process.wait(timeout=20)
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
        self.assertEqual(commands, [dict(operation='hold', callUUID=ACTIVE, held='true', applicationRequest='true'),
                                    dict(operation='hold', callUUID=ACTIVE, held='false', applicationRequest='true'),
                                    dict(operation='merge', callUUID=ACTIVE, groupWithCallUUID=WAITING, applicationRequest='true'),
                                    dict(operation='unmerge', callUUID=ACTIVE, applicationRequest='true'),
                                    dict(operation='end-held-and-accept', callUUID=WAITING, activeCallUUID=ACTIVE, heldCallUUID=HELD)])


if __name__ == '__main__':
    unittest.main()
