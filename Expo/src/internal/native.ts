import { requireNativeModule, type NativeModule } from 'expo';
import { TurboModuleRegistry, type TurboModule } from 'react-native';

import { ConversationRuntime } from './client';
import type { NativeBridge, NativeEvents } from './protocol';

const runtimeKey = Symbol.for('ios-voip-sdk.conversation-runtime');

/** Keep native subscriptions and in-flight handlers across Fast Refresh. */
export function getRuntime(): ConversationRuntime {
  const existing = Reflect.get(globalThis, runtimeKey) as
    ConversationRuntime | undefined;
  if (existing !== undefined) return existing;
  const bridge = requireNativeModule<NativeModule<NativeEvents> & NativeBridge>(
    'IosVoipSDK',
  );
  if (bridge.mediaAdapter === 'livekit') {
    // Resolve the already registered native engine once; audio authority remains in LCK.
    try {
      if (
        TurboModuleRegistry.get<TurboModule>('VoIPSDKMediaRegistry') === null
      ) {
        console.warn(
          'ios-voip-sdk: the selected LiveKit native media registry is unavailable.',
        );
      }
    } catch {
      console.warn(
        'ios-voip-sdk: the selected LiveKit native media registry could not attach.',
      );
    }
  }
  const runtime = new ConversationRuntime(bridge);
  Object.defineProperty(globalThis, runtimeKey, {
    value: runtime,
    configurable: true,
  });
  return runtime;
}
