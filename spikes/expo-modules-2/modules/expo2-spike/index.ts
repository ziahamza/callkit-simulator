import { NativeModule, SharedObject, requireNativeModule } from 'expo';

export type ConversationId = string & {
  readonly __conversationId: unique symbol;
};
export type Conversation =
  | Readonly<{ id: ConversationId; status: 'ringing'; caller: string }>
  | Readonly<{ id: ConversationId; status: 'active'; connectedAt: number }>;
export type Snapshot = Readonly<{
  version: number;
  conversations: readonly Conversation[];
}>;
export type ChangeEvent = Readonly<{ snapshot: Snapshot; version: number }>;
export type ThreadSample = Readonly<{
  phase: string;
  threadID: string;
  threadName: string;
  queueLabel: string;
  isMainThread: boolean;
  isOnJavaScriptThread: boolean;
  supportsAsyncScheduling: boolean;
}>;
export type SDKReply = Readonly<{
  sequence: number;
  threads: readonly ThreadSample[];
}>;
export type HandlerWire = Readonly<{
  id: string;
  runtime: string;
  kind: string;
  sentMs: number;
  dueMs: number;
}>;
export type HandlerReport = Readonly<{
  id: string;
  runtime: string;
  outcome: string;
  sentMs: number;
  dueMs: number;
  enteredMs: number;
  settledMs: number;
  finishedMs: number;
  returnedMs: number;
  abortedMs: number;
  created: number;
  resumes: number;
  returned: number;
  late: number;
}>;
export type HandlerSnapshot = Readonly<{
  runtime: string;
  checkpoint: string;
  reloads: number;
  pending: number;
  cases: readonly HandlerReport[];
}>;
export type StartupReceipt = Readonly<{
  installedMs: number;
  firstBundleStartMs: number;
  firstReceiptLocked: boolean;
  delegateInstalled: boolean;
  observedLoads: number;
}>;
export type PingEvent = Readonly<{ label: string }>;
export declare class PingProbe extends SharedObject<{ ping: (event: PingEvent) => void }> {
  constructor(label: string);
  ping(): void;
}
export type ClosureProbe = Readonly<{
  times: readonly number[];
  threads: readonly ThreadSample[];
  counts: readonly number[];
}>;
type Events = {
  change: (event: ChangeEvent) => void;
  handler: (event: HandlerWire) => void;
  background: (event: ThreadSample) => void;
};

declare class Expo2SpikeModule extends NativeModule<Events> {
  readonly snapshot: Snapshot;
  readonly PingProbe: typeof PingProbe;
  readonly startupReceipt: StartupReceipt;
  rawAsync(callback: (sentMs: number) => Promise<number>): Promise<number>;
  rawBlocking(callback: (sentMs: number) => number): Promise<ClosureProbe>;
  readonly handlerSnapshot: HandlerSnapshot;
  handlerRegistered(enabled: boolean): void;
  saveHandlerCheckpoint(json: string): void;
  handlerClock(): number;
  handlerMark(id: string, runtime: string, at: number, abort: boolean): void;
  handlerSettle(id: string, runtime: string, success: boolean, at: number): boolean;
  handlerStart(id: string, timeout: number): void;
  resetHazard(): void;
  hazardCount(scheduled: boolean): number;
  hazardIncrement(scheduled: boolean): Promise<ThreadSample>;
  publish(version: number): void;
  configureFixture(count: number): void;
  threadSample(phase: string): ThreadSample;
  defaultProbe(): Promise<readonly ThreadSample[]>;
  sdkProbe(): Promise<SDKReply>;
  emitFromBackground(): void;
  drainChanges(): Promise<void>;
  writeResults(json: string): Promise<void>;
}

export default requireNativeModule<Expo2SpikeModule>('Expo2Spike');
