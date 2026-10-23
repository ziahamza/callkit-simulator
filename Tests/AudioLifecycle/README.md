# Audio lifecycle observer controls

Run from the repository root:

```sh
python3 Tests/AudioLifecycle/check_audio_lifecycle.py
```

Consumer builds require SDK 27 or newer. The helper verifies older-SDK consumer
refusal, then uses `VSSDK_TEST_AUDIO_LIFECYCLE_LEGACY_SDK` only for decoder controls;
SwiftPM and CocoaPods never provide this test-only optout.

The helper compiles the actual Objective-C lifecycle observer against the installed
`iphoneos` and `watchsimulator` SDK headers. Each SDK is checked both normally and
with a preincluded header selecting the pre-27 compiler branch. Undefined-symbol
checks require the new notifications when the installed SDK is 27 or later and
exclude them and their context classes from the older compiler branch. This is compiler coverage;
it does not constitute compilation with an older installed SDK.

The same production observer then executes against macOS Foundation and explicit
AVFAudio notification/context facades. Both compiler-selected paths exercise
decoding, absent and malformed context values, resume recommendations, media
services reset, main-queue delivery from a background post, explicit invalidation,
and deallocation. The new path ignores legacy interruption notifications. The
facades expose no session activation API; an injected activation mutation must
fail compilation so that this constraint is checked rather than assumed.

These are host controls, with no simulator boot, real notification delivery, app
build, device use, or audio engine authorization measurement. Native lifecycle
delivery and the Swift consumer's authorization policy require their own evidence.
All commands, exit codes, output and source/header digests are included in JSON
on standard output. Temporary compiler products are removed when the helper exits.

`python3 Tests/AudioLifecycle/check_audio_fixtures.py` separately checks the two
historical ordinary-session traces. Their RED/no-I/O PASS outcomes do not qualify
the current source, genuine LCK interruptions or device media behavior.
