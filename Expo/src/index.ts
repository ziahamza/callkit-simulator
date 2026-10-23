import { getRuntime } from './internal/native';
import type {
  Conversation,
  ConversationHandler,
  ConversationReport,
  IncomingCallPayload,
  StartConversationOptions,
} from './types';

export * from './types';
export {
  useAudioRoutes,
  useConversation,
  useConversationEvent,
  useConversations,
  usePushToken,
} from './hooks';

export function getConversations(): Promise<readonly Conversation[]> {
  return getRuntime().getConversations();
}

/** Current state follows real route notifications after the native request. */
export function selectAudioRoute(id: string): Promise<void> {
  return getRuntime().bridge.selectAudioRoute(id);
}

/**
 * Replacing/removing the handler aborts the previous handler's in-flight signals.
 * Native keeps unexpired actions pending for the next handler and owns deadlines.
 */
export function setConversationHandler(
  handler: ConversationHandler | null,
): void {
  getRuntime().setHandler(handler);
}

export function startConversation(
  options: StartConversationOptions,
): Promise<string> {
  return getRuntime().bridge.startConversation(options);
}

export function joinConversation(id: string): Promise<void> {
  return getRuntime().bridge.joinConversation(id);
}

export function endConversation(id: string): Promise<void> {
  return getRuntime().bridge.endConversation(id);
}

export function setMuted(id: string, muted: boolean): Promise<void> {
  return getRuntime().bridge.setMuted(id, muted);
}

export function setHeld(id: string, held: boolean): Promise<void> {
  return getRuntime().bridge.setHeld(id, held);
}

export function playTones(id: string, digits: string): Promise<void> {
  return getRuntime().bridge.playTones(id, digits);
}

export function mergeConversations(
  first: string,
  second: string,
): Promise<void> {
  return getRuntime().bridge.mergeConversations(first, second);
}

export function reportConversation(
  id: string,
  report: ConversationReport,
): Promise<void> {
  return getRuntime().bridge.reportConversation(id, report);
}

export function reportIncomingConversation(
  payload: IncomingCallPayload,
): Promise<string> {
  return getRuntime().bridge.reportIncomingConversation(payload);
}
