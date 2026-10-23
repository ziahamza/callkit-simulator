import {
  endConversation,
  getConversations,
  joinConversation,
  reportConversation,
  setConversationHandler,
  setHeld,
  selectAudioRoute,
  type ActionContext,
  type Conversation,
  type ConversationHandler,
  type IncomingCallPayload,
} from 'ios-voip-sdk';

import { journey, type MediaSnapshot } from './journey';

type Fields = Record<string, string | boolean | number | null>;
let traceQueue = Promise.resolve();
let automaticRun: Promise<void> | undefined;

function trace(event: string, fields: Fields): Promise<void> {
  traceQueue = traceQueue.then(() =>
    journey().trace(event, JSON.stringify(fields)),
  );
  return traceQueue;
}

/** Queue fixed hook metadata with this fixture's other evidence writes. */
export function observeMediaEvent(name: string, conversationID?: string): void {
  if (journey().m2Case === '') return;
  // Keep a failed queue rejected for the final result, while handling this
  // fire-and-forget hook promise immediately.
  void trace('m2.hookEvent', {
    name,
    ...(conversationID === undefined ? {} : { conversationID }),
  }).catch(() => {});
}

async function check(
  label: string,
  passed: boolean,
  conversationID?: string,
): Promise<void> {
  await trace('m2.assertion', {
    label,
    passed,
    conversationID: conversationID ?? null,
  });
  if (!passed) throw new Error('Example media assertion failed: ' + label);
}

const sleep = (milliseconds: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, milliseconds));

async function waitForConversations(
  predicate: (snapshot: readonly Conversation[]) => boolean,
  timeoutMS = 8_000,
): Promise<readonly Conversation[]> {
  const deadline = Date.now() + timeoutMS;
  while (Date.now() < deadline) {
    const snapshot = await getConversations();
    if (predicate(snapshot)) return snapshot;
    await sleep(25);
  }
  throw new Error('Example media conversation observation deadline');
}

async function mediaSnapshot(label: string): Promise<MediaSnapshot> {
  // The example native method serializes only its whitelisted primitive fields.
  const snapshot: MediaSnapshot = JSON.parse(
    await journey().mediaSnapshot(label),
  );
  return snapshot;
}

async function waitForMedia(
  label: string,
  predicate: (snapshot: MediaSnapshot) => boolean,
  identity?: MediaSnapshot,
  timeoutMS = 8_000,
): Promise<MediaSnapshot> {
  const deadline = Date.now() + timeoutMS;
  while (Date.now() < deadline) {
    const snapshot = await mediaSnapshot('poll');
    if (
      identity &&
      (snapshot.moduleID !== identity.moduleID ||
        snapshot.deviceID !== identity.deviceID)
    ) {
      throw new Error('Example media engine identity changed');
    }
    if (predicate(snapshot)) {
      const recorded = await mediaSnapshot(label);
      await check(label + '-media', predicate(recorded));
      await check(
        label + '-same-engine',
        !identity ||
          (recorded.moduleID === identity.moduleID &&
            recorded.deviceID === identity.deviceID),
      );
      return recorded;
    }
    await sleep(25);
  }
  throw new Error('Example media engine observation deadline');
}

function handler(
  prepareJoin: (signal: AbortSignal) => Promise<void>,
): ConversationHandler {
  const accept = async (
    kind: string,
    conversation: Conversation,
    context: ActionContext,
  ) => {
    await trace('m2.handler', {
      kind,
      conversationID: conversation.id,
      origin: context.origin,
    });
    if (context.signal.aborted)
      throw new Error('Example media action cancelled');
    // Native manual audio gating has completed; category/mode wait for acceptance.
    // Request capture while its real join action is still pending, so the
    // demand sample proves that capture cannot start before LCK activation.
    if (kind === 'join') await prepareJoin(context.signal);
    if (context.signal.aborted)
      throw new Error('Example media action cancelled');
  };
  return {
    join: (conversation, context) => accept('join', conversation, context),
    start: (conversation, context) => accept('start', conversation, context),
    end: (conversation, context) => accept('end', conversation, context),
    hold: (conversation, context) => accept('hold', conversation, context),
  };
}

function payload(): IncomingCallPayload {
  return {
    eventId: 'sdk-media-event-' + journey().runID,
    serverCallId: 'sdk-media-server-' + journey().runID,
    caller: { id: 'sdk-media-caller', displayName: 'SDK media fixture' },
    hasVideo: false,
  };
}

async function run(): Promise<void> {
  const mode = journey().m2Case;
  let releaseStream: (() => number) | undefined;
  let conversationID: string | undefined;
  let stopRecording: (() => Promise<void>) | undefined;
  let closing = false;
  let success = false;
  try {
    if (mode !== 'livekit' && mode !== 'stale-audio')
      throw new Error('Unknown M2 acceptance case');
    // The none variant must never evaluate optional native media modules.
    const { registerGlobals } = await import('@livekit/react-native');
    const { AudioDeviceModule, mediaDevices } =
      await import('@livekit/react-native-webrtc');
    // One-time public media setup. System activation remains exclusively native.
    registerGlobals({ autoConfigureAudioSession: false });
    const initial = await waitForMedia(
      'initial',
      (sample) =>
        typeof sample.inputAvailable === 'boolean' &&
        typeof sample.outputAvailable === 'boolean' &&
        !sample.isEngineRunning &&
        sample.activatedCount === 0 &&
        sample.deactivatedCount === 0 &&
        sample.missingCount === 0 &&
        sample.failedCount === 0 &&
        sample.routeCount > 0 &&
        sample.category.length > 0 &&
        sample.mode.length > 0,
    );
    setConversationHandler(
      handler(async (signal) => {
        const stream = await mediaDevices.getUserMedia({
          audio: true,
          video: false,
        });
        let released = false;
        let recordingAttempted = false;
        const release = () => {
          if (released) return 0;
          released = true;
          const tracks = stream.getTracks();
          const failures: unknown[] = [];
          for (const track of tracks) {
            try {
              track.stop();
            } catch (error) {
              failures.push(error);
            }
          }
          try {
            stream.release();
          } catch (error) {
            failures.push(error);
          }
          if (failures.length > 0) throw failures[0];
          return tracks.length;
        };
        const checkOpen = () => {
          if (closing || signal.aborted)
            throw new Error('Example media action cancelled');
        };
        try {
          checkOpen();
          releaseStream = release;
          await check(
            'one-local-audio-track',
            stream.getAudioTracks().length === 1,
          );
          await trace('m2.localTrack', {
            count: stream.getAudioTracks().length,
            enabled: stream.getAudioTracks()[0].enabled,
          });
          checkOpen();
          // This requests capture on the existing ADM. It neither grants engine
          // availability nor activates AVAudioSession; native LCK is the authority.
          stopRecording = () => AudioDeviceModule.stopLocalRecording();
          recordingAttempted = true;
          await AudioDeviceModule.startLocalRecording();
          checkOpen();
          await trace('m2.recordingDemand', { accepted: true });
          await waitForMedia(
            'demand',
            (sample) =>
              !sample.inputAvailable &&
              !sample.outputAvailable &&
              !sample.isEngineRunning &&
              sample.category === initial.category &&
              sample.mode === initial.mode &&
              sample.activatedCount === 0 &&
              sample.deactivatedCount === 0 &&
              sample.missingCount === 0 &&
              sample.failedCount === 0,
            initial,
          );
          checkOpen();
        } catch (error) {
          // Cleanup may already have stopped an in-flight start. Stop again
          // after that start settles so late completion cannot restore demand.
          try {
            if (recordingAttempted)
              await AudioDeviceModule.stopLocalRecording();
          } finally {
            release();
          }
          throw error;
        }
      }),
    );
    let inactiveRouteRejected = false;
    try {
      await selectAudioRoute('speaker');
    } catch {
      inactiveRouteRejected = true;
    }
    await check('inactive-route-rejected', inactiveRouteRejected);
    await check('routes-observed', initial.routeCount > 0);
    await journey().awaitMediaControl();
    await journey().simulatePush(
      JSON.stringify({ incomingCall: payload() }),
      true,
      'primary',
    );
    const ringing = await waitForConversations(
      (snapshot) => snapshot.length === 1 && snapshot[0].status === 'ringing',
    );
    conversationID = ringing[0].id;
    await check('push-ringing', true, conversationID);
    await joinConversation(conversationID);
    await waitForConversations(
      (snapshot) =>
        snapshot.length === 1 &&
        snapshot[0]?.id === conversationID &&
        snapshot[0].status === 'connecting' &&
        snapshot[0].pending === undefined &&
        snapshot[0].connectedAt === undefined,
    );
    await check('joined', true, conversationID);
    await reportConversation(conversationID, { type: 'connected' });
    await waitForConversations(
      (snapshot) =>
        snapshot.length === 1 &&
        snapshot[0]?.id === conversationID &&
        snapshot[0].status === 'connected' &&
        snapshot[0].pending === undefined &&
        snapshot[0].connectedAt !== undefined,
    );
    await check('backend-connected', true, conversationID);
    if (mode === 'livekit') {
      await waitForMedia(
        'active',
        (sample) =>
          sample.inputAvailable &&
          sample.outputAvailable &&
          sample.isEngineRunning &&
          sample.activatedCount === 1,
        initial,
      );
      await selectAudioRoute('speaker');
      await waitForMedia(
        'speaker',
        (sample) =>
          sample.currentRouteType === 'Speaker' &&
          sample.currentRouteID?.startsWith('output:') === true &&
          sample.inputAvailable &&
          sample.outputAvailable &&
          sample.isEngineRunning,
        initial,
      );
      await setHeld(conversationID, true);
      await waitForConversations((snapshot) => snapshot[0]?.status === 'held');
      await waitForMedia(
        'held',
        (sample) =>
          !sample.inputAvailable &&
          !sample.outputAvailable &&
          !sample.isEngineRunning &&
          sample.deactivatedCount === 1,
        initial,
      );
      await setHeld(conversationID, false);
      await waitForConversations(
        (snapshot) => snapshot[0]?.status === 'connected',
      );
      await waitForMedia(
        'resumed',
        (sample) =>
          sample.inputAvailable &&
          sample.outputAvailable &&
          sample.isEngineRunning &&
          sample.activatedCount === 2,
        initial,
      );
      await endConversation(conversationID);
      await waitForConversations((snapshot) => snapshot.length === 0);
      await waitForMedia(
        'ended',
        (sample) =>
          !sample.inputAvailable &&
          !sample.outputAvailable &&
          !sample.isEngineRunning &&
          sample.deactivatedCount === 2 &&
          sample.missingCount === 0 &&
          sample.failedCount === 0,
        initial,
      );
      await check('ended-empty-with-demand', true, conversationID);
    } else {
      // The runner sets the runtime's existing staleAudio fault before this join.
      // Observe the core's real deadlines; do not activate or end it from JS.
      await waitForMedia(
        'failed',
        (sample) =>
          !sample.inputAvailable &&
          !sample.outputAvailable &&
          !sample.isEngineRunning &&
          sample.activatedCount === 0 &&
          sample.missingCount === 1 &&
          sample.failedCount === 1,
        initial,
        25_000,
      );
      await waitForConversations((snapshot) => snapshot.length === 0);
      await check('stale-ended-empty-with-demand', true, conversationID);
    }
    success = true;
  } finally {
    closing = true;
    if (!success && conversationID) {
      try {
        await reportConversation(conversationID, {
          type: 'ended',
          reason: 'failed',
        });
      } catch {}
    }
    if (stopRecording) {
      try {
        await stopRecording();
      } catch {
        success = false;
      }
    }
    if (releaseStream) {
      try {
        await trace('m2.localTrackReleased', { count: releaseStream() });
      } catch {
        success = false;
      }
    }
    try {
      setConversationHandler(null);
    } catch {
      success = false;
    }
    await traceQueue.catch(() => {
      success = false;
    });
    await journey().finish(success);
  }
  if (!success) throw new Error('Example media fixture cleanup failed');
}

/** Example-only local media demand; no room/network media quality claim. */
export function runMediaAcceptance(): Promise<void> {
  automaticRun ??= run();
  return automaticRun;
}
