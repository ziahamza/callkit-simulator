import type {
  AudioRoutes,
  Conversation,
  ConversationEventMap,
  ConversationHandler,
  PushToken,
} from '../types';
import { HandlerDispatcher } from './handler';
import type { NativeBridge } from './protocol';
import { immutableCopy, RevisionStore } from './store';

export const EMPTY_CONVERSATIONS: readonly Conversation[] = Object.freeze([]);
export const EMPTY_AUDIO_ROUTES: AudioRoutes = Object.freeze({
  routes: Object.freeze([]),
  current: null,
});

export class ConversationRuntime {
  readonly conversations = new RevisionStore<readonly Conversation[]>(
    EMPTY_CONVERSATIONS,
  );
  readonly pushToken = new RevisionStore<PushToken | null>(null);
  readonly audioRoutes = new RevisionStore<AudioRoutes>(EMPTY_AUDIO_ROUTES);
  private readonly handlers: HandlerDispatcher;
  private dialRequestListeners = 0;
  private dialRequestGeneration = 0;
  private readonly eventListeners = new Set<
    (name: keyof ConversationEventMap, payload: unknown) => void
  >();

  constructor(readonly bridge: NativeBridge) {
    this.handlers = new HandlerDispatcher(bridge);
    // These subscriptions must precede the first asynchronous snapshot read.
    bridge.addListener('conversationsChanged', (event) => {
      this.conversations.accept(event.revision, event.conversations);
    });
    bridge.addListener('pushTokenUpdated', (event) => {
      if (this.pushToken.accept(event.revision, event.token)) {
        this.publishEvent('pushTokenUpdated', this.pushToken.getSnapshot());
      }
    });
    bridge.addListener('handlerAction', (event) =>
      this.handlers.dispatch(immutableCopy(event)),
    );
    bridge.addListener('audioRoutesChanged', (event) => {
      this.audioRoutes.accept(event.revision, {
        routes: event.routes,
        current: event.current,
      });
    });
    bridge.addListener('actionCancelled', (event) =>
      this.handlers.cancel(event),
    );
    bridge.addListener('conversationEvent', (event) => {
      this.publishEvent(event.type, immutableCopy(event.payload));
    });
  }

  startConversations(): void {
    this.conversations.start(() => this.readConversations());
  }

  startPushToken(): void {
    this.pushToken.start(async () => {
      const event = await this.bridge.getPushToken();
      this.pushToken.accept(event.revision, event.token);
    });
  }

  async getConversations(): Promise<readonly Conversation[]> {
    await this.readConversations();
    return this.conversations.getSnapshot();
  }

  startAudioRoutes(): void {
    this.audioRoutes.start(async () => {
      const event = await this.bridge.getAudioRoutes();
      this.audioRoutes.accept(event.revision, {
        routes: event.routes,
        current: event.current,
      });
    });
  }

  setHandler(handler: ConversationHandler | null): void {
    this.handlers.setHandler(handler);
  }

  subscribeEvent<Name extends keyof ConversationEventMap>(
    name: Name,
    listener: (event: ConversationEventMap[Name]) => void,
  ): () => void {
    const dispatch = (
      eventName: keyof ConversationEventMap,
      payload: unknown,
    ) => {
      if (eventName === name) listener(payload as ConversationEventMap[Name]);
    };
    this.eventListeners.add(dispatch);
    if (name === 'dialRequest' && ++this.dialRequestListeners === 1) {
      this.bridge.setDialRequestListenerAttached(
        true,
        ++this.dialRequestGeneration,
      );
    }
    return () => {
      if (!this.eventListeners.delete(dispatch)) return;
      if (name === 'dialRequest' && --this.dialRequestListeners === 0) {
        this.bridge.setDialRequestListenerAttached(
          false,
          ++this.dialRequestGeneration,
        );
      }
    };
  }

  private async readConversations(): Promise<void> {
    const event = await this.bridge.getConversations();
    this.conversations.accept(event.revision, event.conversations);
  }

  private publishEvent<Name extends keyof ConversationEventMap>(
    name: Name,
    event: ConversationEventMap[Name],
  ): void {
    for (const listener of [...this.eventListeners]) listener(name, event);
  }
}
