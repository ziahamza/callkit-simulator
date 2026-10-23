export type JSONValue =
  null | boolean | number | string | JSONValue[] | { [key: string]: JSONValue };

/** Rejection codes emitted by native conversation commands. */
export type ConversationErrorCode =
  | 'invalid_arguments'
  | 'unknown_conversation'
  | 'unsupported'
  | 'conversation'
  | 'duplicate_sdk_linkage';

export interface ConversationCommandError extends Error {
  readonly code: ConversationErrorCode;
}

/** Narrow a caught native rejection without assuming every thrown value is ours. */
export function isConversationCommandError(
  value: unknown,
): value is ConversationCommandError {
  if (!(value instanceof Error) || !('code' in value)) return false;
  return (
    value.code === 'invalid_arguments' ||
    value.code === 'unknown_conversation' ||
    value.code === 'unsupported' ||
    value.code === 'duplicate_sdk_linkage' ||
    value.code === 'conversation'
  );
}

export interface Participant {
  id: string;
  displayName?: string;
  avatarUrl?: string;
  phoneNumber?: string;
  email?: string;
}

export interface IncomingCallPayload {
  eventId: string;
  serverCallId: string;
  caller: Participant;
  hasVideo: boolean;
  startedAt?: string;
  metadata?: { [key: string]: JSONValue };
}

export type ConversationStatus =
  | 'ringing'
  | 'dialing'
  | 'connecting'
  | 'connected'
  | 'held'
  | 'ending'
  | 'ended';

export type EndedReason =
  | 'local'
  | 'remoteEnded'
  | 'failed'
  | 'unanswered'
  | 'answeredElsewhere'
  | 'declinedElsewhere';

export type ActionOrigin = 'system' | 'app';
export type ConversationActionKind =
  'join' | 'start' | 'end' | 'hold' | 'mute' | 'tones';

export interface PendingAction {
  id: string;
  kind: ConversationActionKind;
  /** Effective application deadline, initially 1.5 seconds before platform timeout. */
  deadline: string;
  origin: ActionOrigin;
  held?: boolean;
  muted?: boolean;
  digits?: string;
}

export interface Conversation {
  id: string;
  direction: 'incoming' | 'outgoing';
  status: ConversationStatus;
  members: Participant[];
  capabilities: {
    hold: boolean;
    merge: boolean;
    tones: boolean;
    video: boolean;
  };
  isMuted: boolean;
  isVideo: boolean;
  connectedAt?: string;
  ended?: { at: string; reason: EndedReason };
  payload?: IncomingCallPayload;
  pending?: PendingAction;
}

export interface ActionContext {
  /** Effective application deadline, initially 1.5 seconds before platform timeout. */
  deadline: string;
  signal: AbortSignal;
  origin: ActionOrigin;
}

type ActionHandler<Context extends ActionContext = ActionContext> = (
  conversation: Conversation,
  context: Context,
) => void | Promise<void>;

export interface ConversationHandler {
  join: ActionHandler;
  start: ActionHandler;
  end: ActionHandler;
  hold?: ActionHandler<ActionContext & { held: boolean }>;
  mute?: ActionHandler<ActionContext & { muted: boolean }>;
  tones?: ActionHandler<ActionContext & { digits: string }>;
}

export interface StartConversationOptions {
  to: Participant;
  video: boolean;
  metadata?: { [key: string]: JSONValue };
}

export type ConversationReport =
  | { type: 'connecting' }
  | { type: 'connected' }
  | { type: 'updated'; members?: Participant[]; video?: boolean }
  | { type: 'ended'; reason: EndedReason };

export interface PushToken {
  /** This iOS module supplies apns-voip; fcm preserves the shared RFC type. */
  type: 'apns-voip' | 'fcm';
  value: string;
}

export interface AudioRoute {
  readonly id: string;
  readonly name: string;
  /** AVAudioSession's raw port type value on iOS. */
  readonly type: string;
}

export interface AudioRoutes {
  readonly routes: readonly AudioRoute[];
  readonly current: AudioRoute | null;
}

/** M2 supplies watchdog signals; M3 supplies dial requests. */
export interface ConversationEventMap {
  audioActivated: { conversationId?: string };
  audioDeactivated: { conversationId?: string };
  audioActivationMissing: { conversationId: string };
  audioActivationFailed: { conversationId: string };
  pushTokenUpdated: PushToken | null;
  dialRequest: { handle: string; video: boolean };
  providerReset: Record<string, never>;
  providerOwnershipLost: Record<string, never>;
}
