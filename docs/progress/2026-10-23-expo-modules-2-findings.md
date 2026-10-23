# Expo Modules 2.0 findings — October 24, 2026

Release simulator spike: Expo 58.0.17, RN 0.88.0, core 58.0.24, macros 0.22.0;
Xcode 27.1 / Swift 6.4, iPhone 17 Pro / iOS 27.0. App/pod minimum: iOS 17.4.
Simulator timing only. Results-file assertions gate
the [spike workflow](https://github.com/ziahamza/ios-voip-sdk/actions/workflows/spike-expo-modules-2.yml);
screenshots are diagnostic. Results/logs remain CI artifacts.
Implemented in [#56](https://github.com/ziahamza/ios-voip-sdk/pull/56),
[#57](https://github.com/ziahamza/ios-voip-sdk/pull/57),
[#58](https://github.com/ziahamza/ios-voip-sdk/pull/58) and
[#59](https://github.com/ziahamza/ios-voip-sdk/pull/59);
[threading CI](https://github.com/ziahamza/ios-voip-sdk/actions/runs/40008477163),
[hazard CI](https://github.com/ziahamza/ios-voip-sdk/actions/runs/40031867400),
[handler CI](https://github.com/ziahamza/ios-voip-sdk/actions/runs/40035458634).

## Threading and snapshots

Placement records pthread identity, main/thread/queue diagnostics and
`runtime.isOnJavaScriptThread()`. “JS” is `com.facebook.react.runtime.JavaScript`;
“pool” is `com.apple.root.user-initiated-qos.cooperative`.

| Observation | Main | Thread/queue | JS thread |
| --- | --- | --- | --- |
| Sync `@JS` entry | No | JS | Yes |
| Default async before first `await` | No | JS | Yes |
| Default actor after suspension | No | pool | No |
| Custom SDK actor before/after suspension | No | pool | No |
| Background native `@Event` emission | No | `expo2.spike.background-event` | No |
| JS event listener | No | JS | Yes |

The default actor's [inline executor](https://github.com/expo/expo/blob/expo-modules-jsi%4058.0.17/packages/expo-modules-jsi/apple/Sources/ExpoModulesJSI/Runtime/JavaScriptActor.swift#L3)
does not pin resumption to the JS thread. With 64 concurrent post-await increments,
all 64 accesses ran off JS; the final count varied across runs (loss is an
observation). `runtime.schedule` gave 0 off-thread accesses and exactly 64; only
that variant gates CI. SDK-actor commands returned sequences 1–9.

Snapshots retain a frozen JS handle, encoded once per version; a plain record
getter would encode O(n). For 1,000 warm reads, 2 versus 2,048 conversations gave
p50 0.167 / 0.125 µs and p99 0.333 / 0.291 µs. Publication is excluded;
these are observations, not budgets. Coalesced events deliver versions `[3, 4]`,
reuse identity, and ignore duplicate revisions.

## Lifetimes, types and startup

Resolve, throw, never-settle/deadline and real runtime reload each create/resume/
return exactly one owned native continuation; late settlements are rejected and
pending ends at zero. A native deadline Task emits cancellation into an AbortSignal,
including cancel-before-invoke. Observed local invoke latency was 0.064–0.160 ms,
JS-to-native return 4.39–27.60 ms, and deadline-to-abort 11.57 ms
(timer lateness included; dispatch-only 0.157 ms). No-handler policy applies immediately without continuations or JS dispatch.

Bounded raw async callbacks resolve/reject; a value-returning callback blocks its
background native caller for 15.14 ms. Raw async waiters have [no teardown
cancellation](https://github.com/expo/expo/blob/a248d78e3b9260e6f39b320ed25ae65e90acff7d/packages/expo-modules-jsi/apple/Tests/JavaScriptCallbackTests.swift#L368).
The safe handler therefore uses a void invoke/cancel event and explicit settlement;
reload cancels natively before teardown, then the incoming runtime publishes
the surviving value-only ledger.

`scan-exports` schema 8 matches the actual TypeScript compiler view, including
records, unions, branded IDs, callbacks, events and Expo base classes. Five real
declaration mutations fail CI. Typed `@Event` on `@SharedObject` works.
An AppDelegateSubscriber retains a real PKPushRegistry, sets its delegate, and
requests VoIP registration 389.35 ms before RN's native bundle-load START. Both timestamps
are recorded and survive reload unchanged. The native cold default also precedes
initial loading. PushKit requires CallKit linkage.

**M3.5: GO with a thin macro adapter and native-owned lifetimes.** Keep SDK state
in its chosen isolated service, cross it with Sendable values, schedule all JSI
publication explicitly, and retain versioned frozen projections. Register PushKit
and apply cold-push defaults natively without waiting for JS. Use the one-shot
request/settle protocol for handlers; default-actor affinity and raw untrusted
async-callback cancellation are NO-GO. Keep the 1.0 fallback while this beta and
device integration mature; these simulator results support beginning #50's adapter.
