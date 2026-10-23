# Optional Expo WebRTC audio

The plugin selects `mediaAdapter: "webrtc"` (default) or `"none"`. Prebuild writes
the choice to both `VoIPSDK.mediaAdapter` and `ios-voip-sdk.mediaAdapter` in the pod
properties. Regenerate pods after changing it. A mismatched compiled selection
throws before SDK startup; repeated launches reuse one selected adapter and the
process's sole SDK owner. `none` leaves media integration to the application.

The SDK adds no WebRTC framework dependency. A WebRTC app must already link an
implementation of the public `RTCAudioSession` contract. Missing classes or
selectors fail preparation cleanly. Native Swift consumers use the core
`MediaAdapter` protocol; there is no additional Swift WebRTC product.

Prepare room/capture demand in the SDK start/join handler, after checking its
cancellation signal. The SDK gates manual audio before the handler and configures
category/mode after acceptance, immediately before native fulfillment. Both use
the common `AudioSessionQueue` with the WebRTC configuration lock held in one
synchronous operation. Genuine LCK callbacks alone authorize media. The SDK
never calls `setActive`, and no JavaScript method can grant audio authorization.
Configure microphone permission and `voip`/`audio` background modes for capture;
the plugin adds `audio` for its WebRTC selection and preserves existing modes.

The example defaults to `none`. `SDK_EXAMPLE_MEDIA_ADAPTER=webrtc` enables its
standard `react-native-webrtc` 155.0.1 / JitsiWebRTC 155.0.1 build variant. The
[pinned podspec](https://github.com/react-native-webrtc/react-native-webrtc/blob/fb662c9dfeb7dbf5b259f92ad070870a7cfc1402/react-native-webrtc.podspec)
and [public audio contract](https://github.com/jitsi/webrtc/blob/623fcfe044d5037b3ecd92001c2ef80ca1d932d5/sdk/objc/components/audio/RTCAudioSession.h)
are the version targets for its build-only typed API/link check. This is a
standard WebRTC backend, not the LiveKit fork.

Facade controls exercise SDK policy and queue ordering. The separate real-backend
CI preparation builds unsigned Release iphoneos, checks exact resolved versions
and a defined backend class, and retains the existing CallKit/runtime exclusions.
Neither check qualifies real call audio, capture flow or device behavior. Native
execution and current-head CI remain pending until those runs actually pass.
