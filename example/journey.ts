import { requireNativeModule, type NativeModule } from 'expo';

export interface MediaSnapshot {
  label: string;
  moduleID: string;
  deviceID: string;
  inputAvailable: boolean;
  outputAvailable: boolean;
  isEngineRunning: boolean;
  routeCount: number;
  currentRouteID: string | null;
  currentRouteType: string | null;
  category: string;
  mode: string;
  activatedCount: number;
  deactivatedCount: number;
  missingCount: number;
  failedCount: number;
}

type JourneyModule = NativeModule & {
  acceptanceEnabled: boolean;
  m3Enabled: boolean;
  m3DirectEnabled: boolean;
  m1Case: '' | 'journey' | 'cold-launch' | 'unhandled-expiry';
  systemCase:
    | ''
    | 'answer-end'
    | 'decline'
    | 'hold-accept'
    | 'end-accept'
    | 'mute-echo'
    | 'provider-reset'
    | 'push-launch';
  m2Case: '' | 'livekit' | 'stale-audio';
  runID: string;
  nextSystemCommand(): Promise<{
    id: string;
    operation: string;
    conversationID?: string;
  }>;
  acceptance(): Promise<void>;
  dialListenerReady(): Promise<void>;
  deliverDirectDialRequests(): Promise<void>;
  observedDialRequest(handle: string, video: boolean): Promise<void>;
  /** Simulator-only, example-only public getters on the existing selected ADM. */
  mediaSnapshot(label: string): Promise<string>;
  /** Runner-bound sandbox control receipt, simulator-only. */
  awaitMediaControl(): Promise<void>;
  simulatePush(
    payloadJSON: string,
    mustReport: boolean,
    label: 'primary' | 'cold',
  ): Promise<string>;
  waitForColdStart(): Promise<{
    conversationID: string;
    actionID: string;
    deadline: string;
  }>;
  trace(event: string, fieldsJSON: string): Promise<void>;
  finish(success: boolean): Promise<void>;
};

// The native fixture lives only in this example, outside the SDK package.
export function journey(): JourneyModule {
  return requireNativeModule<JourneyModule>('VoIPExampleJourney');
}
