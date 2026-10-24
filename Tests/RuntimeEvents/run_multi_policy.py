#!/usr/bin/env python3
"""Run the engine's pure multi-call control policy on the host, without iOS."""
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    engine = (ROOT / 'Sources/Engine/RuntimeEngine.swift').read_text()
    matches = re.findall(r'private struct MultiCallPolicy \{\n.*?\n\}', engine, re.S)
    assert len(matches) == 1, 'the engine must expose one pure multi-call control policy'
    tests = r'''
@main struct MultiCallPolicyTests {
  static func main() {
    func id(_ suffix: String) -> UUID { UUID(uuidString: "00000000-0000-4000-8000-0000000000" + suffix)! }
    let a = id("01"), b = id("02"), c = id("03"), d = id("04")
    let groupA = id("11"), groupB = id("12"), groupC = id("13")
    func call(_ id: UUID, _ group: UUID, connected: Bool = true, ended: Bool = false,
              capabilities: Set<String> = ["pausing", "merging", "unmerging"]) -> MultiCallPolicy.Call {
      MultiCallPolicy.Call(id: id, group: group, connected: connected, ended: ended, capabilities: capabilities)
    }
    let separate = [a: call(a, groupA), b: call(b, groupB), c: call(c, groupC)]
    let supported: Set<String> = ["pausing", "merging", "unmerging"]
    precondition(MultiCallPolicy.capabilityRejection(.hold, source: supported) == nil)
    precondition(MultiCallPolicy.capabilityRejection(.hold, source: []) == "call does not support pausing")
    precondition(MultiCallPolicy.capabilityRejection(.merge(b), source: [], target: supported) == "call does not support merging")
    precondition(MultiCallPolicy.capabilityRejection(.merge(b), source: supported, target: []) == "group target does not support merging")
    precondition(MultiCallPolicy.capabilityRejection(.merge(b), source: supported, target: supported) == nil)
    precondition(MultiCallPolicy.capabilityRejection(.unmerge, source: []) == "call does not support unmerging")
    precondition(MultiCallPolicy.capabilityRejection(.unmerge, source: supported) == nil)
    func rejection(_ operation: MultiCallPolicy.Operation, _ id: UUID,
                   _ calls: [UUID: MultiCallPolicy.Call], groups: Int = 3, perGroup: Int = 3,
                   groupChangePending: Bool = false) -> String? {
      MultiCallPolicy.rejection(operation, callID: id, calls: calls,
                                maximumCallGroups: groups, maximumCallsPerCallGroup: perGroup,
                                groupChangePending: groupChangePending)
    }
    precondition(rejection(.merge(c), a, separate, perGroup: 2, groupChangePending: true) == "group change already pending")
    precondition(rejection(.merge(c), b, separate, perGroup: 2, groupChangePending: true) == "group change already pending",
                 "distinct selected calls cannot reserve the same stale target capacity concurrently")
    precondition(rejection(.unmerge, a, separate, groupChangePending: true) == "group change already pending")
    precondition(rejection(.hold, a, separate, groupChangePending: true) == nil,
                 "a pending group change must not block independent hold controls")
    precondition(rejection(.hold, a, separate) == nil)
    precondition(rejection(.merge(b), a, separate) == nil)
    precondition(rejection(.merge(a), a, separate) == "merge target must be another call")
    precondition(rejection(.merge(d), a, separate) == "unknown group target")
    precondition(rejection(.unmerge, d, separate) == "unknown call")
    var ringing = separate
    ringing[b] = call(b, groupB, connected: false)
    precondition(MultiCallPolicy.capabilityRejection(.hold, source: ringing[b]!.capabilities) == nil,
                 "application capability validation must not add a connection-state requirement")
    precondition(rejection(.merge(b), a, ringing) == "group target must be connected")
    precondition(rejection(.hold, b, ringing) == "call must be connected")
    var ended = separate
    ended[b] = call(b, groupB, ended: true)
    precondition(rejection(.merge(b), a, ended) == "unknown group target")
    var limited = separate
    limited[a] = call(a, groupA, capabilities: [])
    precondition(rejection(.hold, a, limited) == "call does not support pausing")
    precondition(rejection(.merge(b), a, limited) == "call does not support merging")
    precondition(rejection(.unmerge, a, limited) == "call does not support unmerging")
    limited = separate
    limited[b] = call(b, groupB, capabilities: ["pausing"])
    precondition(rejection(.merge(b), a, limited) == "group target does not support merging")
    let pair = [a: call(a, groupA), b: call(b, groupA), c: call(c, groupC)]
    let conference = [a: call(a, groupA), b: call(b, groupA), c: call(c, groupA)]
    precondition(rejection(.unmerge, a, conference, groups: 2) == nil)
    precondition(rejection(.unmerge, b, conference, groups: 2, groupChangePending: true) == "group change already pending",
                 "a second split cannot consume capacity reserved by the pending first split")
    let afterFirstSplit = [a: call(a, groupB), b: call(b, groupA), c: call(c, groupA)]
    precondition(rejection(.unmerge, b, afterFirstSplit, groups: 2) == "maximumCallGroups reached")
    precondition(rejection(.merge(c), a, pair, perGroup: 1) == "maximumCallsPerCallGroup reached")
    precondition(rejection(.merge(c), a, pair, perGroup: 2) == nil,
                 "only the selected call moves; its source peer must not consume target capacity")
    precondition(rejection(.unmerge, a, pair, groups: 2) == "maximumCallGroups reached")
    precondition(rejection(.unmerge, b, pair, groups: 2) == "maximumCallGroups reached")
    precondition(rejection(.unmerge, a, pair, groups: 3) == nil)
    precondition(rejection(.unmerge, c, pair, groups: 2) == nil,
                 "detaching a singleton replaces one group rather than adding a group")
    precondition(rejection(.merge(b), a, pair, perGroup: 2) == nil,
                 "a selected call already in the target group must be counted once")
    let triple = [a: call(a, groupA), b: call(b, groupA), c: call(c, groupA), d: call(d, groupB)]
    precondition(rejection(.merge(a), d, triple, perGroup: 3) == "maximumCallsPerCallGroup reached")
    print("passed: live targets, capabilities, selected-call capacity, anchor/member split capacity, singleton detach")
  }
}
'''
    with tempfile.TemporaryDirectory(prefix='runtime-multi-policy-') as temporary:
        directory = Path(temporary)
        source, binary = directory / 'policy.swift', directory / 'policy'
        source.write_text('import Foundation\n' + matches[0] + '\n' + tests)
        compiled = subprocess.run(['xcrun', 'swiftc', '-swift-version', '6', '-warnings-as-errors',
                                   '-parse-as-library', str(source), '-o', str(binary)],
                                  capture_output=True, text=True, timeout=60)
        if compiled.returncode:
            raise RuntimeError(compiled.stdout + compiled.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
        if result.returncode or not result.stdout.startswith('passed:'):
            raise RuntimeError(result.stdout + result.stderr)
        print(json.dumps(dict(success=True, classification='host policy; multi-call device behavior unverified',
                              result=result.stdout.strip())))


if __name__ == '__main__':
    main()
