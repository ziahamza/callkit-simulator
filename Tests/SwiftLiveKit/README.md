# Real Swift LiveKit engine fixture — native acceptance pending

This fixture targets LiveKit Swift **2.18.0**, commit
`93a7251057bdb6a80a7b1f6d11464d578ebdc09b`, with its actual pinned WebRTC and
UniFFI wrappers. `Package.resolved` is a previous genuine consumer resolution used
as a build input; it is not evidence of a new build or run. The builder resolves
and verifies the fresh checked-out dependency revisions before linking.

With a native execution lease, build from the clean committed checkout:

```sh
python3 discovery/build_swift_livekit_contract.py /absolute/fresh/output
```

The builder uses an owned temporary SwiftPM consumer, the current root package and
unchanged optional adapter bytes. It links fresh simulator package objects into
`SDKSwiftLiveKit.app` (`dev.iosvoipsdk.swiftlivekit`), embeds its dynamic frameworks
and package resources, and removes intermediates. It does not launch anything.
The manifest is a build template; the copied consumer supplies its source target.
The root SDK gains no product or dependency.

The separate `swift-livekit-native` Xcode27.1 CI job builds this consumer from
current committed source, then runs both modes on one new owned simulator:

```sh
python3 discovery/run_swift_livekit.py /absolute/fresh/build /absolute/fresh/capture
python3 discovery/swift_livekit_conformance.py /absolute/fresh/build /absolute/fresh/capture
```

The runner preserves inherited DYLD entries and refuses external SDK runtime
injection. It queries/validates the installed app's data container once, grants
only its microphone privacy permission, and uses two fresh run IDs/processes.
The shared `run_scenario` supervisor supplies the launcher PID acknowledgement,
owned PID stop/absence proof and raw host-PTY console. The final combined checkout
must retain main's #41 supervisor repairs and the optional
`--inherit-launcher-group` helper boundary. The driver creates a private supervisor
session; the opt-in console inherits that group. Actual host process-group
identities and absence are required separately from the app PID. Failure cleanup
masks parent TERM/INT, reaps for25s, and may kill only the verified owned group
before a final5s reap; forced cleanup cannot qualify. Default helper launches
retain their existing session behavior. This isolated preparation does not
replace that shared helper. Cleanup always attempts owned shutdown/delete and separately
lists devices to prove absence. Source/build/pin/framework/executable/session/
journal/fault/cleanup bindings are required; missing evidence fails acceptance.

The handler checks automatic configuration is disabled and availability is `.none`
before enabling **manual rendering/no-device mode** and requesting local recording.
It keeps LiveKit's real default audio-session/mixer observer chain. The active
join/resume checkpoints require the actual weakly observed manual `AVAudioEngine.isRunning == true`;
the default `audioSession` and `mixer` observers remain first in the same chain.
The observer captures only a weak identity and reads engine state on the existing
AudioSessionQueue after the synchronous setter returns. The public
`AudioManager.isEngineRunning` scalar is retained separately as `publicRunning`:
it may describe the device engine rather than the manual engine. Callback counts,
availability and API success are insufficient running proof. Hold/end must read false for
engine running and both availability flags. Every SDK/fixture LiveKit access runs
on `AudioSessionQueue`; no session activation, Room, server, playback or physical
microphone/audio-quality claim is involved. Failure to observe the engine is RED.

The `stale-audio` process waits at `livekit.readyForFault`. The runner sends the
existing runtime mailbox `faults` request using its freshly built cksim-notify;
this test-only transport reuses the actual request/response grammar without
another CLI container query. Both original request and ACK are retained and bound
to the same command/run/PID/bundle. The fixture must consume that ACK before join,
and the native journal must record `fault.audioSuppressed` for this sole call.
A generic configuration ACK cannot qualify the fault.
The explicitly logged test policy is **1s + 1s**, not the
proposed default 10s + 5s. Exactly one recovery, both same-call watchdog events,
empty final state and a never-enabled engine are required. Final demand is stopped.

Prepared host controls use explicit inert facades; API compilation, those controls
and Expo's different ADM journey do not qualify this real Swift engine fixture.
Fresh dependency build/link, both native executions and CI remain **unmeasured**.
`python3 discovery/check_swift_livekit.py` exercises synthetic positive/negative
gate controls only; it never builds or launches Apple software.
