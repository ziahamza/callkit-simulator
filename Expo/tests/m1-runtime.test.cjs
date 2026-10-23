const assert = require('node:assert/strict');
const { after, test } = require('node:test');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

// Compile only the dependency-free bridge logic; no Expo/native mocks or app.
const root = path.resolve(__dirname, '../..');
const output = mkdtempSync(path.join(tmpdir(), 'ios-voip-m1-ts-'));
after(() => rmSync(output, { recursive: true, force: true }));
execFileSync(
  process.execPath,
  [
    path.join(root, 'node_modules/typescript/bin/tsc'),
    '--target',
    'ES2022',
    '--module',
    'commonjs',
    '--moduleResolution',
    'node',
    '--strict',
    '--skipLibCheck',
    '--rootDir',
    'Expo/src',
    '--outDir',
    output,
    'Expo/src/internal/client.ts',
  ],
  { cwd: root, stdio: 'pipe' },
);
const { ConversationRuntime } = require(
  path.join(output, 'internal/client.js'),
);
const { isConversationCommandError } = require(path.join(output, 'types.js'));

test('native command rejection guard retains only declared codes', () => {
  for (const code of ['invalid_arguments', 'unknown_conversation', 'unsupported', 'conversation', 'duplicate_sdk_linkage']) {
    const error = Object.assign(new Error('native rejection'), { code });
    assert.equal(isConversationCommandError(error), true);
  }
  for (const value of [null, undefined, 'conversation', new Error('plain'),
    { code: 'conversation', message: 'plain object' },
    Object.assign(new Error('foreign'), { code: 'foreign' })]) {
    assert.equal(isConversationCommandError(value), false);
  }
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function native() {
  const listeners = new Map();
  return {
    attachments: [],
    settlements: [],
    dialAttachments: [],
    setDialRequestListenerAttached(attached, generation) {
      this.dialAttachments.push({ attached, generation });
    },
    addListener(name, callback) {
      const callbacks = listeners.get(name) ?? new Set();
      callbacks.add(callback);
      listeners.set(name, callbacks);
      return { remove: () => callbacks.delete(callback) };
    },
    emit(name, value) {
      for (const callback of listeners.get(name) ?? []) callback(value);
    },
    getConversations: async () => ({ revision: 0, conversations: [] }),
    getPushToken: async () => ({ revision: 0, token: null }),
    getAudioRoutes: async () => ({ revision: 0, routes: [], current: null }),
    setHandlerAttached(attached, generation) {
      this.attachments.push({ attached, generation });
    },
    settleAction(...args) {
      this.settlements.push(args);
    },
  };
}

function conversation(id = 'call') {
  return {
    id,
    direction: 'incoming',
    status: 'ringing',
    members: [{ id: 'caller' }],
    capabilities: { hold: true, merge: true, tones: true, video: true },
    isMuted: false,
    isVideo: false,
    payload: {
      eventId: 'event',
      serverCallId: 'server',
      caller: { id: 'caller' },
      hasVideo: false,
      metadata: { nested: [{ value: 'original' }] },
    },
  };
}

function action(overrides = {}) {
  return {
    actionID: 'action',
    generation: 1,
    kind: 'join',
    conversation: conversation(),
    deadline: '2026-10-22T02:00:00.000Z',
    origin: 'system',
    ...overrides,
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const handler = (join = async () => {}) => ({
  join,
  start: async () => {},
  end: async () => {},
});

test('subscribes before hydration; stale reads and equal revisions preserve the latest stable snapshot', async () => {
  const bridge = native();
  const initial = deferred();
  bridge.getConversations = () => {
    bridge.emit('conversationsChanged', {
      revision: 2,
      conversations: [conversation('new')],
    });
    return initial.promise;
  };
  const runtime = new ConversationRuntime(bridge);
  let changes = 0;
  const unsubscribe = runtime.conversations.subscribe(() => changes++);
  const read = runtime.getConversations();
  const latest = runtime.conversations.getSnapshot();
  initial.resolve({ revision: 1, conversations: [conversation('old')] });
  assert.equal(await read, latest);
  assert.equal(latest[0].id, 'new');
  bridge.emit('conversationsChanged', { revision: 2, conversations: [] });
  assert.equal(runtime.conversations.getSnapshot(), latest);
  assert.equal(changes, 1);
  unsubscribe();
  bridge.emit('conversationsChanged', { revision: 3, conversations: [] });
  assert.equal(changes, 1);
});

test('snapshots are independent deeply frozen copies, including opaque metadata', async () => {
  const bridge = native();
  const runtime = new ConversationRuntime(bridge);
  const original = conversation();
  bridge.emit('conversationsChanged', {
    revision: 0,
    conversations: [original],
  });
  original.payload.metadata.nested[0].value = 'changed outside the store';
  const snapshot = runtime.conversations.getSnapshot();
  assert.equal(snapshot[0].payload.metadata.nested[0].value, 'original');
  assert.ok(Object.isFrozen(snapshot));
  assert.ok(Object.isFrozen(snapshot[0].payload.metadata.nested[0]));
  assert.throws(() => snapshot.push(conversation()), TypeError);
});

test('nullable token invalidation beats stale hydration and emits one public event', async () => {
  const bridge = native();
  const initial = deferred();
  bridge.getPushToken = () => initial.promise;
  const runtime = new ConversationRuntime(bridge);
  const events = [];
  const unsubscribe = runtime.subscribeEvent('pushTokenUpdated', (event) =>
    events.push(event),
  );
  runtime.startPushToken();
  bridge.emit('pushTokenUpdated', { revision: 2, token: null });
  initial.resolve({
    revision: 1,
    token: { type: 'apns-voip', value: 'fixture' },
  });
  await flush();
  assert.equal(runtime.pushToken.getSnapshot(), null);
  assert.deepEqual(events, [null]);
  bridge.emit('pushTokenUpdated', {
    revision: 2,
    token: { type: 'apns-voip', value: 'stale' },
  });
  assert.deepEqual(events, [null]);
  unsubscribe();
});

test('hydration failure is observable; a newer native event recovers the snapshot', async () => {
  const bridge = native();
  bridge.getConversations = async () => {
    throw new Error('fixture unavailable');
  };
  const runtime = new ConversationRuntime(bridge);
  let changes = 0;
  runtime.conversations.subscribe(() => changes++);
  runtime.startConversations();
  await flush();
  assert.throws(
    () => runtime.conversations.getSnapshot(),
    /fixture unavailable/,
  );
  bridge.emit('conversationsChanged', { revision: 0, conversations: [] });
  assert.deepEqual(runtime.conversations.getSnapshot(), []);
  assert.equal(changes, 2);
});

test('handler resolution/rejection settles once; missing optional actions default to accept', async () => {
  const bridge = native();
  const runtime = new ConversationRuntime(bridge);
  let calls = 0;
  let context;
  runtime.setHandler(
    handler(async (_conversation, ctx) => {
      calls++;
      context = ctx;
    }),
  );
  bridge.emit('handlerAction', action());
  bridge.emit('handlerAction', action());
  await flush();
  assert.equal(calls, 1);
  assert.equal(context.deadline, action().deadline);
  assert.equal(context.origin, 'system');
  assert.equal(context.signal.aborted, false);
  assert.deepEqual(bridge.settlements, [['action', 1, true, undefined]]);
  for (const [kind, field, value] of [
    ['hold', 'held', true],
    ['mute', 'muted', false],
    ['tones', 'digits', '12#'],
  ]) {
    bridge.emit(
      'handlerAction',
      action({ actionID: kind, kind, [field]: value }),
    );
  }
  await flush();
  assert.equal(bridge.settlements.length, 4);
  runtime.setHandler(
    handler(() => {
      throw new Error('fixture rejected');
    }),
  );
  bridge.emit('handlerAction', action({ actionID: 'rejected', generation: 2 }));
  await flush();
  assert.deepEqual(bridge.settlements.at(-1), [
    'rejected',
    2,
    false,
    { name: 'Error', message: 'fixture rejected' },
  ]);
});

test('native cancellation aborts the signal and prevents late fulfilment', async () => {
  const bridge = native();
  const runtime = new ConversationRuntime(bridge);
  const pending = deferred();
  let signal;
  runtime.setHandler(
    handler((_conversation, ctx) => {
      signal = ctx.signal;
      return pending.promise;
    }),
  );
  bridge.emit('handlerAction', action());
  await flush();
  bridge.emit('actionCancelled', {
    actionID: 'action',
    generation: 1,
    reason: 'deadline',
  });
  assert.equal(signal.aborted, true);
  assert.equal(signal.reason, 'deadline');
  pending.resolve();
  await flush();
  assert.deepEqual(bridge.settlements, []);
});

test('replacement/removal abort prior work, ignores old generations and permits native redelivery', async () => {
  const bridge = native();
  const runtime = new ConversationRuntime(bridge);
  const first = deferred();
  let oldSignal;
  runtime.setHandler(
    handler((_conversation, ctx) => {
      oldSignal = ctx.signal;
      return first.promise;
    }),
  );
  bridge.emit('handlerAction', action());
  await flush();
  let replacementCalls = 0;
  runtime.setHandler(
    handler(async () => {
      replacementCalls++;
    }),
  );
  assert.equal(oldSignal.aborted, true);
  bridge.emit('handlerAction', action());
  bridge.emit('actionCancelled', {
    actionID: 'action',
    generation: 1,
    reason: 'late',
  });
  bridge.emit('handlerAction', action({ generation: 2 }));
  first.resolve();
  await flush();
  assert.equal(replacementCalls, 1);
  assert.deepEqual(bridge.settlements, [['action', 2, true, undefined]]);
  runtime.setHandler(null);
  bridge.emit('handlerAction', action({ actionID: 'detached', generation: 3 }));
  await flush();
  assert.equal(replacementCalls, 1);
  assert.deepEqual(bridge.attachments, [
    { attached: true, generation: 1 },
    { attached: true, generation: 2 },
    { attached: false, generation: 3 },
  ]);
});

test('cancellation before the scheduled callback never invokes app code', async () => {
  const bridge = native();
  const runtime = new ConversationRuntime(bridge);
  let calls = 0;
  runtime.setHandler(
    handler(async () => {
      calls++;
    }),
  );
  bridge.emit('handlerAction', action());
  bridge.emit('actionCancelled', {
    actionID: 'action',
    generation: 1,
    reason: 'cancelled',
  });
  await flush();
  assert.equal(calls, 0);
  assert.deepEqual(bridge.settlements, []);
});

test('abort listeners can supersede replacement/removal without a stale native attachment', async () => {
  for (const nestedRemoval of [true, false]) {
    const bridge = native();
    const runtime = new ConversationRuntime(bridge);
    let nestedCalls = 0;
    const nested = nestedRemoval
      ? null
      : handler(async () => {
          nestedCalls++;
        });
    runtime.setHandler(
      handler((_conversation, context) => {
        context.signal.addEventListener(
          'abort',
          () => runtime.setHandler(nested),
          { once: true },
        );
        return new Promise(() => {});
      }),
    );
    bridge.emit('handlerAction', action());
    await flush();
    runtime.setHandler(nestedRemoval ? handler() : null);
    assert.deepEqual(bridge.attachments, [
      { attached: true, generation: 1 },
      { attached: !nestedRemoval, generation: 3 },
    ]);
    bridge.emit('handlerAction', action({ generation: 3 }));
    await flush();
    assert.equal(nestedCalls, nestedRemoval ? 0 : 1);
    assert.equal(bridge.settlements.length, nestedRemoval ? 0 : 1);
  }
});

test('non-state signals are routed by name and unsubscribe removes the listener', () => {
  const bridge = native();
  const runtime = new ConversationRuntime(bridge);
  const events = [];
  const unsubscribe = runtime.subscribeEvent('providerReset', (value) =>
    events.push(value),
  );
  bridge.emit('conversationEvent', { type: 'audioActivated', payload: {} });
  bridge.emit('conversationEvent', { type: 'providerReset', payload: {} });
  unsubscribe();
  bridge.emit('conversationEvent', { type: 'providerReset', payload: {} });
  assert.deepEqual(events, [{}]);
});

test('dial request readiness follows actual public listeners, including synchronous cold drain', () => {
  const bridge = native();
  const request = { handle: '+15555550142', video: true };
  bridge.setDialRequestListenerAttached = function (attached, generation) {
    this.dialAttachments.push({ attached, generation });
    if (attached)
      this.emit('conversationEvent', { type: 'dialRequest', payload: request });
  };
  const runtime = new ConversationRuntime(bridge);
  runtime.subscribeEvent('audioActivated', () => {});
  assert.deepEqual(bridge.dialAttachments, []);
  const seen = [];
  const first = runtime.subscribeEvent('dialRequest', (event) =>
    seen.push(event),
  );
  assert.deepEqual(seen, [request]);
  assert.deepEqual(bridge.dialAttachments, [{ attached: true, generation: 1 }]);
  const second = runtime.subscribeEvent('dialRequest', () => {});
  first();
  first();
  assert.equal(bridge.dialAttachments.length, 1);
  second();
  assert.deepEqual(bridge.dialAttachments[1], {
    attached: false,
    generation: 2,
  });
  const third = runtime.subscribeEvent('dialRequest', () => {});
  assert.deepEqual(bridge.dialAttachments[2], {
    attached: true,
    generation: 3,
  });
  third();
});

test('audio route event wins over stale hydration and preserves stable immutable state', async () => {
  const bridge = native();
  const initial = deferred();
  bridge.getAudioRoutes = () => initial.promise;
  const runtime = new ConversationRuntime(bridge);
  let changes = 0;
  runtime.audioRoutes.subscribe(() => changes++);
  runtime.startAudioRoutes();
  const output = { id: 'output:speaker-uid', name: 'Speaker', type: 'Speaker' };
  bridge.emit('audioRoutesChanged', {
    revision: 2,
    routes: [output],
    current: output,
  });
  const current = runtime.audioRoutes.getSnapshot();
  initial.resolve({ revision: 1, routes: [], current: null });
  await flush();
  assert.equal(runtime.audioRoutes.getSnapshot(), current);
  assert.deepEqual(current, { routes: [output], current: output });
  assert.ok(Object.isFrozen(current));
  assert.ok(Object.isFrozen(current.routes));
  assert.ok(Object.isFrozen(current.routes[0]));
  assert.ok(Object.isFrozen(current.current));
  output.name = 'Changed upstream';
  assert.equal(current.current.name, 'Speaker');
  assert.equal(current.routes[0].name, 'Speaker');
  assert.equal(changes, 1);
  bridge.emit('audioRoutesChanged', { revision: 2, routes: [], current: null });
  assert.equal(runtime.audioRoutes.getSnapshot(), current);
});

test('audio route loss stays null after a delayed initial read', async () => {
  const bridge = native();
  const initial = deferred();
  bridge.getAudioRoutes = () => initial.promise;
  const runtime = new ConversationRuntime(bridge);
  runtime.startAudioRoutes();
  bridge.emit('audioRoutesChanged', { revision: 3, routes: [], current: null });
  initial.resolve({
    revision: 0,
    routes: [{ id: 'output:old-uid', name: 'Old', type: 'Headphones' }],
    current: { id: 'output:old-uid', name: 'Old', type: 'Headphones' },
  });
  await flush();
  assert.deepEqual(runtime.audioRoutes.getSnapshot(), {
    routes: [],
    current: null,
  });
});
