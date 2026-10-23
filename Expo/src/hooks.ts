import { useEffect, useRef, useSyncExternalStore } from 'react';

import { EMPTY_AUDIO_ROUTES, EMPTY_CONVERSATIONS } from './internal/client';
import { getRuntime } from './internal/native';
import type {
  AudioRoutes,
  Conversation,
  ConversationEventMap,
  PushToken,
} from './types';

/** System-maintained state. Snapshots are immutable and stable until changed. */
export function useConversations(): readonly Conversation[] {
  const runtime = getRuntime();
  useEffect(() => runtime.startConversations(), [runtime]);
  return useSyncExternalStore(
    runtime.conversations.subscribe,
    runtime.conversations.getSnapshot,
    () => EMPTY_CONVERSATIONS,
  );
}

export function useConversation(id: string): Conversation | undefined {
  return useConversations().find((conversation) => conversation.id === id);
}

/** null means PushKit has not supplied a token, or has invalidated it. */
export function usePushToken(): PushToken | null {
  const runtime = getRuntime();
  useEffect(() => runtime.startPushToken(), [runtime]);
  return useSyncExternalStore(
    runtime.pushToken.subscribe,
    runtime.pushToken.getSnapshot,
    () => null,
  );
}

/** OS-reported inputs/current output plus the explicit speaker override target. */
export function useAudioRoutes(): AudioRoutes {
  const runtime = getRuntime();
  useEffect(() => runtime.startAudioRoutes(), [runtime]);
  return useSyncExternalStore(
    runtime.audioRoutes.subscribe,
    runtime.audioRoutes.getSnapshot,
    () => EMPTY_AUDIO_ROUTES,
  );
}

export function useConversationEvent<Name extends keyof ConversationEventMap>(
  name: Name,
  listener: (event: ConversationEventMap[Name]) => void,
): void {
  const current = useRef(listener);
  useEffect(() => {
    current.current = listener;
  }, [listener]);
  useEffect(
    () => getRuntime().subscribeEvent(name, (event) => current.current(event)),
    [name],
  );
}
