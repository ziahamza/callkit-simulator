# LiveKit Swift integration

Add `LiveKitMediaAdapter.swift` to the app target that links `VoIPSDK` and the
LiveKit product. The root package has no media dependency or additional product.
This integration targets [LiveKit Swift 2.18.0](https://github.com/livekit/client-sdk-swift/tree/93a7251057bdb6a80a7b1f6d11464d578ebdc09b).
The Expo adapter separately controls the existing React Native ADM; do not use
this Swift file with `@livekit/react-native`.

```swift
let application = try VoIPApplication.start(mediaAdapter: LiveKitMediaAdapter())
let calls = application.client
```

Start on MainActor and retain the process's single application/client. Construction
does no backend work. A real start/join action prepares external audio ownership
and engine gating before the handler runs; category/mode are set after acceptance,
immediately before native fulfillment. Create room/capture demand
only inside that prepared handler, after checking its cancellation signal. Do not
call `prepare()` at launch, connect a room, or capture before this preparation.

The adapter disables LiveKit automatic configuration before any backend availability
operation. It publishes readiness after successful, cancellation-checked preparation;
only genuine LCK activation permits `.default`. Deactivation, interruption and reset
revoke authorization and enqueue `.none` on the common `AudioSessionQueue` executor.
Repeated preparation preserves an already authorized call. The SDK never calls
`setActive`; do not independently activate the session or restore LiveKit automatic
configuration. Disabling automatic configuration cannot undo an earlier activation.
The app owns permissions, room/signaling, tracks and their final cleanup.

All SDK-owned LiveKit calls use the common queue, off MainActor and the cooperative
pool. Availability errors propagate; revocation errors are logged. A blocking native
operation cannot be preempted, and availability is not proof of media flow.

`Tests/AudioCore/run_livekit.sh` checks the actual adapter against explicit inert
facades. `Tests/SwiftLiveKit` prepares a separate real pinned consumer/engine journey
in manual rendering mode with no device I/O, Room, server or playback. Its native
execution remains pending; neither facade checks nor API compilation qualifies that
journey. See that fixture's README for the required engine observations.
