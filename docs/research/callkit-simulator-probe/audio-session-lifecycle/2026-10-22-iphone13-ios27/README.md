# Historical ordinary audio fixture

**Measured historical outcome: RED.** On iPhone 13 / iOS 27.0, the ordinary silent-engine experiment requested three operations. After async activation the engine stopped without frame progress; the fourth operation never ran. Raw Apple, C-decoded and Swift-consumed notifications agree: two active and three inactive observations per stream.

`Tests/AudioLifecycle/check_audio_fixtures.py` consumes only the unchanged `device-trace.jsonl`. Counts describe this recording. [Original packet and probe](https://github.com/ziahamza/ios-voip-sdk/tree/ab37b3ed928d9753f61cbc06c9ef769ab31e5d4a/docs/research/callkit-simulator-probe/audio-session-lifecycle/2026-10-22-iphone13-ios27) remain in immutable history. These rows do not qualify current shipping-source equivalence, LCK interruptions, resumption recommendation behavior, stall bounds, A0 or M2.
