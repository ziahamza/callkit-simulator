import type {
  ActionOrigin,
  Conversation,
  ConversationEventMap,
  ConversationReport,
  IncomingCallPayload,
  PushToken,
  StartConversationOptions,
} from '../types';

export interface ConversationEnvelope {
  revision: number;
  conversations: Conversation[];
}

export interface PushTokenEnvelope {
  revision: number;
  token: PushToken | null;
}

interface HandlerActionBase {
  actionID: string;
  generation: number;
  conversation: Conversation;
  deadline: string;
  origin: ActionOrigin;
}

export type HandlerAction = HandlerActionBase &
  (
    | { kind: 'join' | 'start' | 'end' }
    | { kind: 'hold'; held: boolean }
    | { kind: 'mute'; muted: boolean }
    | { kind: 'tones'; digits: string }
  );

export interface ActionCancellation {
  actionID: string;
  generation: number;
  reason: string;
}

export type ConversationEvent = {
  [Name in Exclude<keyof ConversationEventMap, 'pushTokenUpdated'>]: {
    type: Name;
    payload: ConversationEventMap[Name];
  };
}[Exclude<keyof ConversationEventMap, 'pushTokenUpdated'>];

export type NativeEvents = {
  conversationsChanged: (event: ConversationEnvelope) => void;
  pushTokenUpdated: (event: PushTokenEnvelope) => void;
  handlerAction: (event: HandlerAction) => void;
  actionCancelled: (event: ActionCancellation) => void;
  conversationEvent: (event: ConversationEvent) => void;
};

export interface NativeBridge {
  readonly mediaAdapter: 'webrtc' | 'livekit' | 'none';
  addListener<Name extends keyof NativeEvents>(
    name: Name,
    listener: NativeEvents[Name],
  ): { remove(): void };
  getConversations(): Promise<ConversationEnvelope>;
  getPushToken(): Promise<PushTokenEnvelope>;
  setHandlerAttached(attached: boolean, generation: number): void;
  settleAction(
    actionID: string,
    generation: number,
    success: boolean,
    error?: { name: string; message: string },
  ): void;
  startConversation(options: StartConversationOptions): Promise<string>;
  joinConversation(id: string): Promise<void>;
  endConversation(id: string): Promise<void>;
  setMuted(id: string, muted: boolean): Promise<void>;
  setHeld(id: string, held: boolean): Promise<void>;
  playTones(id: string, digits: string): Promise<void>;
  mergeConversations(first: string, second: string): Promise<void>;
  reportConversation(id: string, report: ConversationReport): Promise<void>;
  reportIncomingConversation(payload: IncomingCallPayload): Promise<string>;
}
