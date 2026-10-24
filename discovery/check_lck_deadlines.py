#!/usr/bin/env python3
"""Compile the production profile selector; bind LCK timing to device evidence."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    profile_path = ROOT / 'Sources/Engine/Resources/ios-27.0.json'
    profile = json.loads(profile_path.read_text())
    assert profile.get('lckActionDeadlineSeconds') == 30, 'missing separate measured LCK 30-second deadline'
    evidence = profile['lckActionDeadlineEvidence']
    directory = 'docs/research/callkit-simulator-probe/livecommunicationkit/'
    expected = {directory + '2026-10-21-iphone13-ios27-lck-' + case + '.jsonl': count
                for case, count in [('basic', 10), ('cx-after', 0), ('lck-after', 10), ('two-managers', 4)]}
    assert set(evidence['captures']) == set(expected), 'require all four committed LCK captures'
    for name, count in expected.items():
        path = ROOT / name
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        samples = [row for row in rows if row['event'] == 'lck.perform']
        assert evidence['captures'][name] == dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), samples=count)
        assert len(samples) == count and all(row['timeoutIn'] == 30 for row in samples)
        if not count:
            assert any(row['event'] == 'lck.cx.perform' for row in rows), 'CX takeover must remain explicit'
    pending = {
        'ha-pending-join': ('Join', 63991),
        'ha-pending-pause': ('Pause', 5329),
        'ea-pending-end': ('End', 5330),
    }
    for case, (kind, expiry_ms) in pending.items():
        name = directory + 'multicall/' + case + '.jsonl'
        path = ROOT / name
        assert profile['lckMultiCall']['captureSHA256'][name] == hashlib.sha256(path.read_bytes()).hexdigest()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        timeouts = [row for row in rows if row['event'] == 'lckmulti.timedOut'
                    and row.get('phase') == 'entry' and row['kind'] == kind]
        assert len(timeouts) == 1, 'require one measured pending-action expiry'
        timeout = timeouts[0]
        delivered = [row for row in rows if row['event'] == 'lckmulti.perform'
                     and row['action'] == timeout['action'] and row['kind'] == kind]
        assert len(delivered) == 1 and delivered[0]['timeoutIn'] == 30000
        assert timeout['mono_ms'] - delivered[0]['mono_ms'] == expiry_ms
    engine = (ROOT / 'Sources/Engine/RuntimeEngine.swift').read_text()
    production = engine[engine.index('private enum ProviderAPIKind:'):engine.index('private final class ProviderRegistration:')]
    validation_start = engine.index('    let durations = ')
    validation = engine[validation_start:engine.index('    let requestedRun = ', validation_start)]
    with tempfile.TemporaryDirectory(prefix='cksim-lck-deadlines-') as temporary:
        out = Path(temporary)
        source = out / 'profile.swift'
        source.write_text('import Foundation\n' + production + '''
private enum BootstrapError: Error { case invalid(String) }
private func validate(_ profile: BehaviorProfile) throws {
  let profileID = "ios-27.0"
''' + validation + '''
}
private let profile = try JSONDecoder().decode(BehaviorProfile.self,
  from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
try validate(profile)
private let original = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
for key in ["lckActionDeadlineSeconds", "pauseEndExpirySeconds", "joinExpirySeconds",
            "cleanupEndExpirySeconds", "pausedAfterDeliverySeconds", "joinedAfterSettlementSeconds",
            "audioHandoffGapSeconds", "audioDeactivationAfterDeliverySeconds", "appResumeEndDelaySeconds"] {
 for value: Any in [0.0, -1.0, "Infinity", "-Infinity", "NaN"] {
  var raw = try JSONSerialization.jsonObject(with: original) as! [String: Any]
  if key == "lckActionDeadlineSeconds" { raw[key] = value }
  else {
    var multi = raw["lckMultiCall"] as! [String: Any]
    multi[key] = value
    raw["lckMultiCall"] = multi
  }
  let data = try JSONSerialization.data(withJSONObject: raw)
  let decoder = JSONDecoder()
  // Test-only decoding lets non-finite values reach the exact production guard.
  // The shipping decoder already refuses these strings before that guard.
  decoder.nonConformingFloatDecodingStrategy = .convertFromString(
    positiveInfinity: "Infinity", negativeInfinity: "-Infinity", nan: "NaN")
  let invalid = try decoder.decode(BehaviorProfile.self, from: data)
  var refused = false
  do { try validate(invalid) } catch { refused = true }
  precondition(refused, "invalid LCK duration was accepted: " + key)
 }
}
for (answer, start, pauseOrEnd, expiry) in [(false, false, false, 30.0), (true, false, false, 64.0),
                                         (false, true, false, 30.0), (false, false, true, 5.3)] {
  let lck = profile.timing(api: .livecommunicationkit, answer: answer, start: start, pauseOrEnd: pauseOrEnd)
  precondition(lck.deadline == 30 && lck.expiry == expiry)
}
let answer = profile.timing(api: .callkit, answer: true, start: false)
let start = profile.timing(api: .callkit, answer: false, start: true)
let ordinary = profile.timing(api: .callkit, answer: false, start: false, pauseOrEnd: true)
precondition(answer.deadline == 60 && answer.expiry == 59.58)
precondition(start.deadline == 600 && start.expiry == 600)
precondition(ordinary.deadline == 5 && ordinary.expiry == 5.2)
print("passed")
''')
        binary = out / 'profile'
        subprocess.run(['swiftc', '-swift-version', '6', '-warnings-as-errors', str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary), str(profile_path)], check=True)
    print(json.dumps(dict(success=True, classification='device-evidence and exact production profile host controls; no native expiry measurement',
                          measuredLCKSamples=24, routingOnlyCapture='cx-after',
                          measuredLCKExpirySamples=3, invalidLCKDurationControls=45,
                          profileSHA256=hashlib.sha256(profile_path.read_bytes()).hexdigest())))


if __name__ == '__main__':
    main()
