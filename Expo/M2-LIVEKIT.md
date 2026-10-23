# Optional Expo LiveKit audio

Select `mediaAdapter: "livekit"` in the plugin and regenerate the iOS project/pods.
The native adapter uses the existing React Native WebRTCModule/ADM and observer;
it creates no factory and exports no JavaScript activation control. Its selected
pod pins `@livekit/react-native-webrtc` 150.1.0 (LiveKitWebRTC 150.7871.03).

Before any room/capture, call `registerGlobals({ autoConfigureAudioSession: false })`
from `@livekit/react-native`. Disabling it later cannot undo an existing activation.
Create media demand inside the SDK's prepared start/join handler after checking
cancellation. Native preparation disables automatic session configuration/hooks
and gates availability before the handler. After acceptance, category/mode are
set immediately before fulfillment on the common audio queue. Only genuine
LCK authorization permits the engine. Registry readiness waits are bounded by the
real action's cancellation/deadline. The SDK never calls `setActive`.

The example's `SDK_EXAMPLE_MEDIA_ADAPTER=livekit` variant pins React Native LiveKit
3.1.0, its WebRTC fork 150.1.0 and livekit-client 2.23.0; `none` and standard WebRTC
variants disable this fork's native autolinking. Set the variant for both prebuild
and pod installation. The local-demand fixture requests no Room/server connection.
Facade controls are host policy checks. The dedicated iOS27.1 CI job builds the
pinned Release consumer, inspects its Release iphoneos app for CallKit exclusion,
and requires the real ADM to run after genuine LCK join/resume and stop on
hold/end. Its second case suppresses LCK activation and requires scoped watchdog
events and clean termination while local demand remains. It measures local
demand/availability/running state, not Room signaling or media quality. This
acceptance wiring is proposed until a CI run passes; API/facade compilation does
not satisfy it.
