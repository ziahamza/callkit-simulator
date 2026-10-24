"""Run real InspectorModel multi-call commands against a hermetic journal bridge."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[2]
CALLS = [f'{index:08X}-1111-4111-8111-111111111111' for index in (1, 2, 3)]
GROUPS = [f'{index:08X}-AAAA-4AAA-8AAA-AAAAAAAAAAAA' for index in (1, 2, 3)]
HARNESS = r'''
import Foundation
@main @MainActor struct MultiCallModelTest {
  static func wait(_ condition: () -> Bool) async throws {
    let limit = Date().addingTimeInterval(5)
    while !condition() {
      precondition(Date() < limit, "Model journal confirmation timed out")
      try await Task.sleep(for: .milliseconds(10))
    }
  }
  static func emit(_ value: String) { FileHandle.standardOutput.write(Data((value + "\n").utf8)) }
  static func main() async throws {
    let model = InspectorModel(environment: ["CKSIM_INSPECTOR_URL": CommandLine.arguments[1], "CKSIM_INSPECTOR_TOKEN": "multi-token"])
    let following = Task { await model.follow() }
    defer { following.cancel() }
    let firstID = "00000001-1111-4111-8111-111111111111"
    let targetID = "00000002-1111-4111-8111-111111111111"
    try await wait { model.online && model.conversations.count == 3 }
    precondition(model.groupedConversations.count == 3)
    precondition(model.providerConfigurations.first?.maximumCallGroups == 3 &&
                 model.providerConfigurations.first?.maximumCallsPerCallGroup == 3 &&
                 model.providerConfigurations.first?.apiKind == "callkit")
    for index in 1...4 {
      let call = model.conversations[firstID]!
      let target = model.conversations[targetID]!
      let priorHeld = call.held
      let priorGroup = call.group
      switch index {
      case 1: model.send("hold", call: call, held: true)
      case 2: model.send("hold", call: call, held: false)
      case 3: model.send("merge", call: call, groupTarget: target)
      default: model.send("unmerge", call: call)
      }
      precondition(model.busy)
      try await wait { model.timeline.contains { $0.event == "test.commandAccepted" && $0.requestID == "command-\(index)" } }
      precondition(model.conversations[firstID]?.held == priorHeld && model.conversations[firstID]?.group == priorGroup,
                   "Accepted HTTP controls must not optimistically change call state")
      emit("deliver:\(index)")
      try await wait { !model.busy && model.isPending(model.conversations[firstID]!) }
      let delivered = model.recentSystemTransactions.first { $0.id == "transaction-\(index)" }
      precondition(delivered?.constituents.map(\.actionID) == ["action-\(index)"])
      if index == 3 {
        precondition(model.isPending(model.conversations[targetID]!), "A pending merge must disable its explicit target")
      }
      precondition(model.commandError == nil)
      emit("settle:\(index)")
      try await wait { !model.isPending(model.conversations[firstID]!) }
      if index <= 2 { precondition(model.conversations[firstID]?.held == (index == 1)) }
      if index == 3 { precondition(model.conversations[firstID]?.group == model.conversations[targetID]?.group && model.groupedConversations.count == 2) }
      if index == 4 { precondition(model.conversations[firstID]?.group != model.conversations[targetID]?.group && model.groupedConversations.count == 3) }
    }
    emit("paired")
    try await wait { model.timeline.contains { $0.event == "action.settled" && $0.actionID == "pair-first" } }
    precondition(model.pending["pair-first"] == nil && model.pending["pair-second"] != nil)
    let paired = model.recentSystemTransactions.first { $0.id == "paired" }
    precondition(paired?.constituents.map(\.actionID) == ["pair-first", "pair-second"])
    precondition(paired?.verification.contains("Unverified on device") == true)
    precondition(model.isPending(model.conversations[firstID]!) && model.isPending(model.conversations[targetID]!),
                 "All transaction participants remain pending until every constituent settles")
    emit("finish")
    try await wait { !model.isPending(model.conversations[firstID]!) && !model.isPending(model.conversations[targetID]!) }
  }
}
'''


class MultiCallModelTests(unittest.TestCase):
    def test_commands_and_transaction_wide_pending_use_the_actual_model(self):
        with tempfile.TemporaryDirectory(prefix='inspector-multi-model-') as directory:
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
        session = dict(schemaVersion=1, bundleID='fixture', runID='host-multi', pid=123, profile='ios-27.0')
        rows, commands, failures, output = [], [], [], []
        lock = threading.Lock()
        calls = [dict(uuid=identifier, group=group, connected=True, ringing=False, ended=False, held=False,
                      capabilities=['pausing', 'merging', 'unmerging']) for identifier, group in zip(CALLS, GROUPS)]
        def append(event, **fields):
            rows.append(dict(session, sequence=len(rows) + 1, t_ms=(len(rows) + 1) * 10, event=event, **fields))
        append('provider.created', provider='p1', maximumCallGroups=3, maximumCallsPerCallGroup=3)
        append('provider.apiKind', provider='p1', apiKind='callkit')
        for call in calls:
            append('call.changed', **call)
        def action(identifier, call, kind, transaction, **fields):
            return dict(actionID=identifier, callUUID=call, kind=kind, transactionID=transaction,
                        origin='system', provider='p1', apiKind='callkit', **fields)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, value):
                if self.headers.get('Authorization') != 'Bearer multi-token':
                    failures.append('Bearer token was not forwarded')
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                path = urlsplit(self.path)
                if path.path != '/events': failures.append('Unexpected GET ' + self.path)
                after = int(parse_qs(path.query)['after'][0])
                with lock: value = dict(session=session, events=rows[after:], cursor=len(rows))
                self.respond(value)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path != '/command' or self.headers.get('Content-Type') != 'application/json':
                    failures.append('Unexpected command route or content type')
                with lock:
                    commands.append(body)
                    index = len(commands)
                    append('test.commandAccepted', requestID=f'command-{index}')
                self.respond(dict(success=True, result=dict(actionIDs=[f'action-{index}'], transactionID=f'transaction-{index}')))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        process = subprocess.Popen([str(binary), f'http://127.0.0.1:{server.server_port}'], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        def read_output():
            for line in process.stdout:
                output.append(line)
                with lock:
                    if line.startswith(('deliver:', 'settle:')):
                        stage, number = line.strip().split(':')
                        index = int(number)
                        kind = 'hold' if index <= 2 else 'group'
                        parameters = {'held': index == 1} if index <= 2 else {'groupTargetUUID': CALLS[1] if index == 3 else ''}
                        fields = action(f'action-{index}', CALLS[0], kind, f'transaction-{index}', **parameters)
                        if stage == 'deliver':
                            append('transaction.delivered', transactionID=f'transaction-{index}', origin='system', provider='p1',
                                   actionIDs=[f'action-{index}'], callUUIDs=[CALLS[0]],
                                   kinds=['CXSetHeldCallAction' if index <= 2 else 'CXSetGroupCallAction'], parameters=[parameters])
                            append('action.deadline', **fields)
                        else:
                            if index <= 2: calls[0]['held'] = index == 1
                            else: calls[0]['group'] = GROUPS[1] if index == 3 else GROUPS[0]
                            append('call.changed', **calls[0])
                            append('action.settled', fulfilled=True, **fields)
                    elif line.strip() == 'paired':
                        append('transaction.delivered', transactionID='paired', origin='system', provider='p1',
                               actionIDs=['pair-first', 'pair-second'], callUUIDs=CALLS[:2],
                               kinds=['CXSetHeldCallAction', 'CXAnswerCallAction'], parameters=[{'held': True}, {}])
                        append('action.deadline', **action('pair-first', CALLS[0], 'hold', 'paired', held=True))
                        append('action.deadline', **action('pair-second', CALLS[1], 'answer', 'paired'))
                        append('action.settled', fulfilled=True, **action('pair-first', CALLS[0], 'hold', 'paired', held=True))
                    elif line.strip() == 'finish':
                        append('action.settled', fulfilled=True, **action('pair-second', CALLS[1], 'answer', 'paired'))
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
        self.assertEqual(commands, [dict(operation='hold', callUUID=CALLS[0], held='true'),
                                    dict(operation='hold', callUUID=CALLS[0], held='false'),
                                    dict(operation='merge', callUUID=CALLS[0], groupWithCallUUID=CALLS[1]),
                                    dict(operation='unmerge', callUUID=CALLS[0])])


if __name__ == '__main__':
    unittest.main()
