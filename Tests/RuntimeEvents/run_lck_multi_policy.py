#!/usr/bin/env python3
"""Compile the exact LCK policy on the host; no simulator or synthetic SDK."""
import json
import base64
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CAPTURES = ROOT / 'docs/research/callkit-simulator-probe/livecommunicationkit/multicall'


def rows(name):
    return [json.loads(line) for line in (CAPTURES / (name + '.jsonl')).read_text().splitlines()]


def trace_replays():
    """Derive action identities/order from the original device recordings."""
    replays = []
    for name in ('ha-delay-pause', 'ha-reverse', 'ha-fail-pause', 'ha-fail-join',
                 'ha-pending-pause', 'ha-pending-join', 'ea-delay-end', 'ea-fail-end',
                 'ea-pending-end', 'third-end-held-accept'):
        capture = rows(name)
        accepted = 'C' if name == 'third-end-held-accept' else 'B'
        join = next(r for r in capture if r['event'] == 'lckmulti.perform'
                    and r.get('kind') == 'Join' and r.get('conv') == accepted)
        constituents = [r for r in capture if r['event'] == 'lckmulti.perform'
                        and abs(r['mono_ms'] - join['mono_ms']) <= 3
                        and r.get('kind') in ('Pause', 'End', 'Join')]
        assert len(constituents) == (3 if accepted == 'C' else 2), (name, constituents)
        action_ids = {r['action'] for r in constituents}
        terminals = [r for r in capture if r['event'] in ('lckmulti.settled', 'lckmulti.timedOut')
                     and r.get('action') in action_ids]
        assert len(terminals) == len(action_ids), (name, terminals)
        terminals.sort(key=lambda r: r['mono_ms'])
        succeeded = all(not r.get('failed', False) and r['event'] != 'lckmulti.timedOut'
                        for r in terminals if r['action'] == join['action'])
        joined = [r for r in capture if r['event'] == 'lckmulti.changed'
                  and r.get('conv') == accepted and r.get('state') == 'joined']
        assert bool(joined) == succeeded, name
        if joined:
            assert joined[0]['mono_ms'] >= terminals[-1]['mono_ms'], name
        if name == 'ha-delay-pause':
            pause = next(r for r in constituents if r['kind'] == 'Pause')
            paused = next(r for r in capture if r['event'] == 'lckmulti.changed'
                          and r.get('conv') == 'A' and r.get('state') == 'paused')
            settled_pause = next(r for r in terminals if r['action'] == pause['action'])
            assert paused['mono_ms'] < settled_pause['mono_ms'], 'Pause state must precede late fulfillment'
        replays.append(dict(name=name, actions=[r['action'] for r in constituents],
                            joiningAction=join['action'], joiningCall=join['conversation'],
                            terminals=[(r['action'], not r.get('failed', False) and r['event'] != 'lckmulti.timedOut')
                                       for r in terminals], succeeded=succeeded))
    # These checks are independent facts from the captures, not model constants.
    no_caps = rows('caps-none')
    assert no_caps[0]['capabilities'] == []
    assert any(r['event'] == 'lckmulti.perform' and r.get('kind') == 'Pause' for r in no_caps)
    for name in ('cap-1x1', 'cap-1x2'):
        failure = next(r for r in rows(name) if r['event'] == 'lckmulti.requestResult' and r.get('kind') == 'Start')
        assert failure['code'] == 7 and failure['domain'] == 'com.apple.CallKit.error.requesttransaction'
    for name in ('merge-b-into-a', 'merge-a-into-b'):
        snapshots = [r for r in rows(name) if r['event'] == 'lckmulti.snapshot']
        before = next(r for r in snapshots if r.get('label') == 'B-after-choice')
        after = next(r for r in snapshots if r.get('label', '').startswith('after-merge-'))
        states = lambda r: {c['uuid']: c['state'] for c in r['conversations']}
        assert states(before) == states(after), 'Merge must not invent a group state'
    for name, kind, expected in (('ha-pending-pause', 'Pause', 5.3), ('ha-pending-join', 'Join', 64),
                                 ('ea-pending-end', 'End', 5.3)):
        capture = rows(name)
        timeout = next(r for r in capture if r['event'] == 'lckmulti.timedOut' and r['kind'] == kind)
        delivered = next(r for r in capture if r['event'] == 'lckmulti.perform' and r['action'] == timeout['action'])
        assert 29900 <= delivered['timeoutIn'] <= 30100, 'advertised deadline must remain 30 s'
        assert abs((timeout['mono_ms'] - delivered['mono_ms']) / 1000 - expected) < 0.15
    failed_pause = rows('ha-fail-pause')
    cleanup = next(r for r in failed_pause if r['event'] == 'lckmulti.perform' and r.get('kind') == 'End' and r.get('conv') == 'A')
    settled = next(r for r in failed_pause if r['event'] == 'lckmulti.settled' and r['action'] == cleanup['action'])
    timeout = next(r for r in failed_pause if r['event'] == 'lckmulti.timedOut' and r['action'] == cleanup['action'])
    assert not settled['failed'] and timeout['mono_ms'] > settled['mono_ms']
    assert abs((timeout['mono_ms'] - cleanup['mono_ms']) / 1000 - 5.2) < 0.2
    swap = rows('swap-app')
    resumed = next(r for r in swap if r['event'] == 'lckmulti.perform' and r.get('kind') == 'Pause' and r.get('isPaused') is False)
    displaced = next(r for r in swap if r['event'] == 'lckmulti.perform' and r.get('kind') == 'End' and r.get('conv') == 'B')
    assert displaced['action'] != resumed['action'] and displaced['mono_ms'] - resumed['mono_ms'] == 91
    return replays


def main():
    engine = (ROOT / 'Sources/Engine/RuntimeEngine.swift').read_text()
    profile = json.loads((ROOT / 'Sources/Engine/Resources/ios-27.0.json').read_text())
    assert profile['lckMultiCall']['verification'] == 'measured'
    assert profile['lckMultiCall']['unverified'], 'measured scope must retain its unverified cases'
    policy = re.findall(r'private struct LCKMultiCallPolicy \{\n.*?\n\}', engine, re.S)
    assert len(policy) == 1, 'one measured LCK policy is required'
    profile_types = [re.findall(pattern, engine, re.S)[0] for pattern in (
        r'private enum ProviderAPIKind: String \{[^\n]+\}',
        r'private struct FulfilledPostEndUnmutePolicy: Decodable \{\n.*?\n\}',
        r'private struct IncomingFilterPolicy: Decodable, Sendable \{\n.*?\n\}',
        r'private struct LCKMultiCallProfile: Decodable \{\n.*?\n\}',
        r'private struct BehaviorProfile: Decodable \{\n.*?\n\}')]
    replays = trace_replays()
    tests = r'''
@main struct MeasuredPolicyTests {
  static func main() {
    func id(_ suffix: String) -> UUID { UUID(uuidString: "00000000-0000-4000-8000-0000000000" + suffix)! }
    let pause = id("01"), join = id("02"), end = id("03"), a = id("11"), b = id("12")
    // ha-delay-pause: Join settles first; it cannot become joined yet.
    var delayed = LCKMultiCallPolicy.Barrier(actions: [pause, join], joining: [join: b])
    precondition(delayed.settle(join, fulfilled: true) == nil)
    precondition(delayed.settle(pause, fulfilled: true) == [b])
    precondition(delayed.settle(pause, fulfilled: true) == nil, "release is once-only")
    // ha-fail-pause/pending-pause: a failed or expired Pause is terminal.
    var failedPause = LCKMultiCallPolicy.Barrier(actions: [pause, join], joining: [join: b])
    precondition(failedPause.settle(join, fulfilled: true) == nil)
    precondition(failedPause.settle(pause, fulfilled: false) == [b])
    // ha-fail-join/pending-join: the accepted call must never become joined.
    var failedJoin = LCKMultiCallPolicy.Barrier(actions: [pause, join], joining: [join: b])
    precondition(failedJoin.settle(join, fulfilled: false) == nil)
    precondition(failedJoin.settle(pause, fulfilled: true) == [])
    // ea-fail-end: failed End does not stop the accepted Join.
    var failedEnd = LCKMultiCallPolicy.Barrier(actions: [end, join], joining: [join: b])
    precondition(failedEnd.settle(end, fulfilled: false) == nil)
    precondition(failedEnd.settle(join, fulfilled: true) == [b])
    var delayedEndAudio = LCKMultiCallPolicy.Handoff()
    precondition(!delayedEndAudio.markGapElapsed(), "ea-delay-end cannot reactivate its still-pending source after the audio gap")
    precondition(delayedEndAudio.markJoined())
    var immediateAudio = LCKMultiCallPolicy.Handoff()
    precondition(!immediateAudio.markJoined(), "an early Join cannot skip the deactivation/activation gap")
    precondition(immediateAudio.markGapElapsed())
    precondition(LCKMultiCallPolicy.failure(.pause) == .endWithCleanup)
    precondition(LCKMultiCallPolicy.failure(.join) == .endWithCleanup)
    precondition(LCKMultiCallPolicy.failure(.end) == .endRegardless)
    precondition(LCKMultiCallPolicy.failure(.merge) == .none)
    precondition(LCKMultiCallPolicy.choices(established: 0, held: 0, active: 0, maximumGroups: 1) == ["answer", "decline"])
    precondition(LCKMultiCallPolicy.choices(established: 1, held: 0, active: 1, maximumGroups: 1) == ["end-and-accept", "decline"])
    precondition(LCKMultiCallPolicy.choices(established: 1, held: 0, active: 1, maximumGroups: 2) == ["hold-and-accept", "end-and-accept", "decline"])
    precondition(LCKMultiCallPolicy.choices(established: 2, held: 1, active: 1, maximumGroups: 2) == ["end-and-accept", "decline", "end-held-and-accept"])
    precondition(!LCKMultiCallPolicy.canStart(established: 1, reservedStarts: 0, maximumGroups: 1))
    precondition(!LCKMultiCallPolicy.canStart(established: 1, reservedStarts: 1, maximumGroups: 2))
    precondition(LCKMultiCallPolicy.canStart(established: 1, reservedStarts: 0, maximumGroups: 2))
    precondition(LCKMultiCallPolicy.shouldEndActiveOnResume(maximumGroups: 2, maximumPerGroup: 1))
    precondition(!LCKMultiCallPolicy.shouldEndActiveOnResume(maximumGroups: 2, maximumPerGroup: 2))
    precondition(!LCKMultiCallPolicy.shouldEndActiveOnResume(maximumGroups: 1, maximumPerGroup: 1))
    precondition(LCKMultiCallPolicy.usesCompositeBarrier(system: true, actionCount: 2, joiningCount: 1))
    precondition(!LCKMultiCallPolicy.usesCompositeBarrier(system: true, actionCount: 1, joiningCount: 1),
                 "single Join must preserve its existing audio scheduling")
    precondition(!LCKMultiCallPolicy.usesCompositeBarrier(system: false, actionCount: 2, joiningCount: 1),
                 "application-authored composites were not measured")
    precondition(!LCKMultiCallPolicy.usesCompositeBarrier(system: true, actionCount: 2, joiningCount: 0),
                 "Start, Mute, tones and grouping must not introduce a barrier audio reconciliation")
    precondition(LCKMultiCallPolicy.resumeEndDelay(nominal: 0.091, elapsedSinceIssued: 0) == 0.091)
    precondition(abs(LCKMultiCallPolicy.resumeEndDelay(nominal: 0.091, elapsedSinceIssued: 0.05) - 0.041) < 0.000001)
    precondition(LCKMultiCallPolicy.resumeEndDelay(nominal: 0.091, elapsedSinceIssued: 2) == 0,
                 "late fulfillment must not add another full delivery-relative delay")
    let profile = try! JSONDecoder().decode(BehaviorProfile.self, from: Data(base64Encoded: "PROFILE_DATA")!)
    precondition(profile.timing(api: .callkit, answer: true, start: false).deadline == 60)
    precondition(profile.timing(api: .callkit, answer: true, start: false).expiry == 59.58)
    precondition(profile.timing(api: .callkit, answer: false, start: false, pauseOrEnd: true).deadline == 5)
    precondition(profile.timing(api: .callkit, answer: false, start: false, pauseOrEnd: true).expiry == 5.2)
    precondition(profile.timing(api: .livecommunicationkit, answer: true, start: false).deadline == 30)
    precondition(profile.timing(api: .livecommunicationkit, answer: true, start: false).expiry == 64)
    precondition(profile.timing(api: .livecommunicationkit, answer: false, start: false, pauseOrEnd: true).expiry == 5.3)
    _ = a
    print("passed: delayed/reversed settlement, failed Pause/Join/End, no capability/group gate, measured choices, reserved outgoing capacity, 2x1 app-resume effect")
  }
}
'''
    replay_source = []
    for replay in replays:
        uuid = lambda value: 'UUID(uuidString: ' + json.dumps(value) + ')!'
        actions = ', '.join(uuid(value) for value in replay['actions'])
        block = ['    var replay = LCKMultiCallPolicy.Barrier(actions: [' + actions +
                 '], joining: [' + uuid(replay['joiningAction']) + ': ' + uuid(replay['joiningCall']) + '])']
        for index, (action, fulfilled) in enumerate(replay['terminals']):
            expected = 'nil' if index < len(replay['terminals']) - 1 else (
                '[' + uuid(replay['joiningCall']) + ']' if replay['succeeded'] else '[]')
            block.append('    precondition(replay.settle(' + uuid(action) + ', fulfilled: ' +
                         str(fulfilled).lower() + ') == ' + expected + ', ' + json.dumps(replay['name']) + ')')
        replay_source.extend(['    do {'] + ['  ' + line for line in block] + ['    }'])
    tests = tests.replace('    _ = a', '\n'.join(replay_source) + '\n    _ = a')
    tests = tests.replace('PROFILE_DATA', base64.b64encode((ROOT / 'Sources/Engine/Resources/ios-27.0.json').read_bytes()).decode())
    with tempfile.TemporaryDirectory(prefix='lck-measured-policy-') as temporary:
        out = Path(temporary)
        source, binary = out / 'policy.swift', out / 'policy'
        source.write_text('import Foundation\n' + '\n'.join(profile_types) + '\n' + policy[0] + '\n' + tests)
        compiled = subprocess.run(['xcrun', 'swiftc', '-swift-version', '6', '-warnings-as-errors',
                                   '-parse-as-library', str(source), '-o', str(binary)],
                                  capture_output=True, text=True, timeout=60)
        if compiled.returncode:
            raise RuntimeError(compiled.stdout + compiled.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
        if result.returncode or not result.stdout.startswith('passed:'):
            raise RuntimeError(result.stdout + result.stderr)
        print(json.dumps(dict(success=True, classification='host policy; device evidence #85', traceReplays=len(replays),
                              result=result.stdout.strip())))


if __name__ == '__main__':
    main()
