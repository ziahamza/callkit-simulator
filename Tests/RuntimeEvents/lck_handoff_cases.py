"""Native LCK audio/dataflow regressions, with device and injected evidence kept distinct.

The delayed-resume rule is an injected scheduler regression, not a newly measured
Apple timing guarantee. It preserves the device-observed End-active composition.
"""
import time

PREFIX = 'lck.fixture.'


def event(j, name, predicate=lambda row: True, *, start=0):
    return j.wait(lambda rows: next((r for r in rows[start:] if r['event'] == PREFIX + name and predicate(r)), None))


def audio_delay(j, settled, low, description):
    activated = event(j, 'didActivate')
    assert activated['sequence'] > settled['sequence'], description + ' audio preceded native fulfillment'
    delay = activated['t_ms'] - settled['t_ms']
    assert low <= delay <= 1000, f'{description} audio after settlement: {delay}ms'
    print(f'LCK {description} audio after settlement: {delay} ms', flush=True)
    return delay


def composite_audio(j, name, operation, delayed_kind):
    j.pair(name)
    j.app('policy', rules={delayed_kind: dict(mode='delay', ms=2000, nth=1)})
    start = len(j.rows())
    action_ids = j.composite(operation)
    assert len(action_ids) == 2
    performed = [r for r in j.rows()[start:] if r['event'] == PREFIX + 'perform' and r.get('actionID') in action_ids]
    assert {(r['kind'], r['callUUID']) for r in performed} == {(delayed_kind, j.ids['A']), ('Join', j.ids['B'])}
    delayed = next(r for r in performed if r['kind'] == delayed_kind)
    j.wait(lambda rows: action_ids <= {r.get('actionID') for r in rows[start:] if r['event'] == PREFIX + 'settled'})
    rows = j.state(A='paused' if delayed_kind == 'Pause' else 'left', B='joined')
    settled = [r for r in rows[start:] if r['event'] == PREFIX + 'settled' and r.get('actionID') in action_ids]
    delayed_settlement = next(r for r in settled if r['actionID'] == delayed['actionID'])
    assert 1900 <= delayed_settlement['t_ms'] - delayed['t_ms'] <= 5000, 'delayed constituent must really settle about 2s late'
    joined_settlement = next(r for r in settled if r['kind'] == 'Join')
    assert joined_settlement['sequence'] < delayed_settlement['sequence'], 'Join must settle while the other constituent remains pending'
    terminal_sequence = max(r['sequence'] for r in settled)
    joined = next(r for r in rows[start:] if r['event'] == PREFIX + 'changed' and r['callUUID'] == j.ids['B'] and r['state'] == 'joined')
    assert joined['sequence'] > terminal_sequence, 'accepted B joined before every constituent settled'
    activated = event(j, 'didActivate', start=start)
    rows = j.rows()[start:]
    assert not any(r['event'] == PREFIX + 'didActivate' and r['sequence'] < terminal_sequence for r in rows), 'composite reactivated audio before its last settlement'
    assert activated['sequence'] > terminal_sequence
    assert any(r['event'] == PREFIX + 'didDeactivate' and r['sequence'] < activated['sequence'] for r in rows), 'native handoff must deactivate before activating'
    delay = activated['t_ms'] - max(r['t_ms'] for r in settled)
    print(f'LCK {name} audio after last native settlement: {delay} ms', flush=True)
    return delay


def run_mute_case(j):
    j.begin('mutePreservesPendingJoinAudio')
    j.app('incoming', call='A')
    action_ids = set(j.system('answer', call='A')['actionIDs'])
    joined = event(j, 'settled', lambda r: r.get('actionID') in action_ids and r['kind'] == 'Join')
    # Proposed regression budgets, not Apple timing measurements: stimulate
    # late enough that restarting the 290ms Join timer is distinguishable.
    observed_at = time.monotonic()
    observed_age_ms = time.time() * 1000 - joined['wallTimeMS']
    assert -5 <= observed_age_ms <= 260, 'Join settlement was observed too late for the Mute timing window'
    stimulus_at = observed_at + max(0, (180 - observed_age_ms) / 1000)
    while (remaining := stimulus_at - time.monotonic()) > 0:
        time.sleep(min(remaining, 0.02))
    command_offset_ms = observed_age_ms + (time.monotonic() - observed_at) * 1000
    assert 150 <= command_offset_ms <= 260, 'host missed the proposed late-Mute stimulus window'
    assert not any(r['event'] == PREFIX + 'didActivate' for r in j.rows()), 'Mute stimulus missed the pending Join audio window'
    mute_ids = set(j.system('mute', call='A', muted=True)['actionIDs'])
    muted = event(j, 'perform', lambda r: r.get('actionID') in mute_ids and r['kind'] in ('Mute', 'MuteConversationAction'))
    mute_settlement = event(j, 'settled', lambda r: r.get('actionID') == muted['actionID'])
    activated = event(j, 'didActivate')
    assert joined['sequence'] < muted['sequence'] < mute_settlement['sequence'] < activated['sequence'], 'Mute must settle while the original Join audio remains pending'
    delivery_ms = muted['t_ms'] - joined['t_ms']
    settlement_ms = mute_settlement['t_ms'] - joined['t_ms']
    assert 150 <= delivery_ms <= 260, f'native Mute delivery missed the proposed late window: {delivery_ms}ms'
    join_audio_ms = audio_delay(j, joined, 280, 'Join with late intervening Mute')
    after_mute_ms = activated['t_ms'] - mute_settlement['t_ms']
    assert 0 <= after_mute_ms <= 240, f'Mute restarted the pending Join audio timer: {after_mute_ms}ms after Mute settlement'
    time.sleep(0.35)
    assert len([r for r in j.rows() if r['event'] == PREFIX + 'didActivate']) == 1, 'Mute caused duplicate audio activation'
    assert not any(r['event'] == PREFIX + 'didDeactivate' for r in j.rows()), 'Mute unexpectedly deactivated audio'
    print(f'LCK late Mute delivery: {delivery_ms} ms; Join audio: {join_audio_ms} ms; audio after Mute settlement: {after_mute_ms} ms', flush=True)
    return dict(joinToMuteDeliveryMS=delivery_ms, joinToMuteSettlementMS=settlement_ms,
                joinToAudioMS=join_audio_ms, muteSettlementToAudioMS=after_mute_ms,
                hostMuteCommandOffsetMS=command_offset_ms, timingEvidence='proposed regression budgets')


def run_cases(j):
    j.begin('singleJoinAudio')
    j.app('incoming', call='A')
    action_ids = set(j.system('answer', call='A')['actionIDs'])
    assert len(action_ids) == 1, 'single Join must contain exactly one runtime action'
    settled = event(j, 'settled', lambda r: r.get('actionID') in action_ids and r['kind'] == 'Join')
    j.state(A='joined')
    # #85's first Join activates hundreds of ms after settlement. This budget
    # catches the composite-only path accidentally making it immediate.
    join_audio_delay = audio_delay(j, settled, 200, 'single Join')

    j.begin('singleStartAudio')
    j.app('start', call='A')
    started = event(j, 'perform', lambda r: r['kind'] == 'Start' and r['callUUID'] == j.ids['A'])
    settled = event(j, 'settled', lambda r: r.get('actionID') == started['actionID'])
    start_audio_delay = audio_delay(j, settled, 150, 'single Start')

    mute_timings = run_mute_case(j)

    hold_audio_delay = composite_audio(j, 'delayedPauseCompositeAudio', 'hold-and-accept', 'Pause')
    end_audio_delay = composite_audio(j, 'delayedEndCompositeAudio', 'end-and-accept', 'End')

    j.pair('delayedResumeEndsActive')
    handoff_start = len(j.rows())
    j.composite()
    j.state(A='paused', B='joined')
    event(j, 'didActivate', start=handoff_start)
    j.app('policy', rules={'Pause': dict(mode='delay', ms=300, nth=1)})
    resume_start = len(j.rows())
    j.app('pause', call='A', paused=False)
    resumed = event(j, 'perform', lambda r: r['kind'] == 'Pause' and r['callUUID'] == j.ids['A'] and r.get('held') is False, start=resume_start)
    settled = event(j, 'settled', lambda r: r.get('actionID') == resumed['actionID'], start=resume_start)
    assert settled.get('policy') == 'delay' and settled.get('failed') is False
    fulfillment_delay = settled['t_ms'] - resumed['t_ms']
    assert 250 <= fulfillment_delay <= 1500, f'resume fixture delay was {fulfillment_delay}ms'
    ended = event(j, 'perform', lambda r: r['kind'] == 'End' and r['callUUID'] == j.ids['B'], start=resume_start)
    assert ended['sequence'] > settled['sequence'], 'system End B arrived before delayed Resume A settled'
    after_settlement = ended['t_ms'] - settled['t_ms']
    assert 0 <= after_settlement <= 150, f'elapsed resume delay was not clamped to zero: {after_settlement}ms'
    observed_b = [r['kind'] for r in j.rows()[resume_start:] if r['event'] == PREFIX + 'perform' and r['callUUID'] == j.ids['B']]
    assert observed_b == ['End'], 'resuming A must end active B, without a Pause B'
    j.state(A='joined', B='left')
    print(f'LCK delayed Resume fulfillment: {fulfillment_delay} ms; End B after settlement: {after_settlement} ms', flush=True)
    j.save()
    return dict(singleJoinAudioMS=join_audio_delay, singleStartAudioMS=start_audio_delay,
                mutePreservedJoinAudioMS=mute_timings['joinToAudioMS'], muteTimings=mute_timings, delayedPauseCompositeAudioMS=hold_audio_delay,
                delayedEndCompositeAudioMS=end_audio_delay, delayedResumeMS=fulfillment_delay,
                endAfterResumeSettlementMS=after_settlement, delayedResumeEvidence='injected scheduler regression')
