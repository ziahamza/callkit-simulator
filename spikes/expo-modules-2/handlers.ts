import { reloadAppAsync } from 'expo';
import { startupBeforeReload, runInterop } from './interop';
import bridge, { HandlerReport, HandlerWire, ThreadSample } from './modules/expo2-spike';
import type { Result } from './demonstrations';

const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
function check(value: unknown, message: string): asserts value {
  if (!value) throw new Error(message);
}
async function waitCase(id: string, runtime: string, ready: (value: HandlerReport) => boolean) {
  const end = performance.now() + 7000;
  while (performance.now() < end) {
    const value = bridge.handlerSnapshot.cases.find((value) => value.id === id && value.runtime === runtime);
    if (value && ready(value)) return value;
    await delay(5);
  }
  throw new Error(`Handler case deadline: ${id}`);
}
type Checkpoint = {
  runtime: string;
  results: Result[];
  threads: ThreadSample[];
  cancelBeforeInvoke: boolean;
  startup: import('./modules/expo2-spike').StartupReceipt;
};

export async function runHandlers(previous: Result[]): Promise<Result[]> {
  const encoded = bridge.handlerSnapshot.checkpoint;
  let saved: Checkpoint | undefined = encoded ? JSON.parse(encoded) : undefined;
  try {
    if (!saved) {
      const startup = await startupBeforeReload();
      const runtime = bridge.handlerSnapshot.runtime;
      let received = 0;
      const threads: ThreadSample[] = [];
      const controllers = new Map<string, AbortController>();
      const cancelled = new Set<string>();
      let handler: ((event: HandlerWire, signal: AbortSignal) => Promise<void>) | undefined;
      const receive = (event: HandlerWire) => {
        const key = event.runtime + ':' + event.id;
        if (event.kind === 'cancel') {
          cancelled.add(key);
          controllers.get(key)?.abort();
          controllers.delete(key);
          return;
        }
        received++;
        const controller = new AbortController();
        controller.signal.addEventListener('abort', () =>
          bridge.handlerMark(event.id, event.runtime, performance.now(), true),
        );
        if (cancelled.has(key)) {
          controller.abort();
          return controller.signal;
        }
        const selected = handler;
        if (!selected) return;
        controllers.set(key, controller);
        void (async () => {
          bridge.handlerMark(event.id, event.runtime, performance.now(), false);
          threads.push(bridge.threadSample('handler-js-' + event.id));
          try {
            await selected(event, controller.signal);
            bridge.handlerSettle(event.id, event.runtime, true, performance.now());
          } catch {
            bridge.handlerSettle(event.id, event.runtime, false, performance.now());
          } finally {
            controllers.delete(key);
          }
        })(); // Void transport: native never subscribes to this untrusted Promise.
        return controller.signal;
      };
      bridge.addListener('handler', receive);
      bridge.handlerRegistered(false);
      bridge.handlerStart('cold', 1000);
      const cold = await waitCase('cold', runtime, (value) => value.returned === 1);
      check(
        cold.outcome === 'default-fail' && cold.created === 0 && cold.resumes === 0 && received === 0,
        'No handler applies native default without a JS dispatch or continuation',
      );
      let staleWork = false;
      handler = async (event) => {
        if (event.id === 'ordering') {
          staleWork = true;
          return;
        }
        if (event.id === 'throw') throw new Error('handler failure');
        if (event.id === 'never' || event.id === 'reload') await new Promise<void>(() => {});
        else await delay(15);
      };
      bridge.handlerRegistered(true);
      const ordering = { id: 'ordering', runtime, kind: 'cancel', sentMs: 0, dueMs: 0 };
      receive(ordering);
      const cancelBeforeInvoke = receive({ ...ordering, kind: 'invoke' })?.aborted === true;
      check(cancelBeforeInvoke && !staleWork, 'Cancel before invoke aborts without starting stale work');
      for (const [id, outcome] of [
        ['resolve', 'resolved'],
        ['throw', 'threw'],
        ['never', 'deadline'],
      ]) {
        bridge.handlerStart(id, id === 'never' ? 250 : 1000);
        const value = await waitCase(
          id,
          runtime,
          (value) => value.returned === 1 && (id !== 'never' || value.abortedMs > 0),
        );
        check(
          value.outcome === outcome && value.created === 1 && value.resumes === 1,
          `${id}: exactly one native resume`,
        );
        check(!bridge.handlerSettle(id, runtime, true, performance.now()), 'Late/duplicate settlement rejected');
      }
      saved = { runtime, results: previous, threads, cancelBeforeInvoke, startup };
      bridge.saveHandlerCheckpoint(JSON.stringify(saved));
      bridge.handlerStart('reload', 5000);
      await waitCase('reload', runtime, (value) => value.enteredMs > 0 && value.outcome === 'pending');
      await reloadAppAsync('expo2 handler lifetime');
      return await new Promise<Result[]>(() => {}); // The incoming JS runtime publishes final results.
    }
    await waitCase('reload', saved.runtime, (value) => value.returned === 1);
    const snapshot = bridge.handlerSnapshot;
    const resumed = saved;
    const cases = snapshot.cases.filter((value) => value.runtime === resumed.runtime);
    check(
      snapshot.runtime !== saved.runtime && snapshot.reloads >= 1 && snapshot.pending === 0,
      'Real runtime reload cancels native waiters',
    );
    check(
      cases.length === 5 &&
        cases.every(
          (value) =>
            value.returned === 1 && value.created === (value.id === 'cold' ? 0 : 1) && value.resumes === value.created,
        ),
      'All owned native continuations resumed exactly once',
    );
    check(cases.find((value) => value.id === 'reload')?.outcome === 'reload', 'Reload beats deadline');
    const never = cases.find((value) => value.id === 'never')!;
    const latencies = cases
      .filter((value) => ['resolve', 'throw', 'never'].includes(value.id))
      .map((value) => ({
        id: value.id,
        invokeMs: value.enteredMs - value.sentMs,
        resolveMs: value.settledMs ? value.returnedMs - value.settledMs : null,
      }));
    const abortAfterDeadlineMs = never.abortedMs - never.dueMs;
    check(
      latencies.every(
        (value) =>
          value.invokeMs >= -0.01 && value.invokeMs < 2000 && (value.resolveMs === null || value.resolveMs >= -0.01),
      ) && abortAfterDeadlineMs >= -0.01,
      'Monotonic invoke/resolve/AbortSignal measurements',
    );
    return [
      ...saved.results,
      ...(await runInterop(saved.startup)),
      {
        id: 'handler',
        pass: true,
        values: {
          cases,
          latencies,
          abortAfterDeadlineMs,
          abortDispatchMs: never.abortedMs - never.finishedMs,
          reloads: snapshot.reloads,
          runtimeChanged: true,
          cancelBeforeInvoke: saved.cancelBeforeInvoke,
        },
        threads: saved.threads,
      },
    ];
  } catch (error) {
    return [
      ...(saved?.results ?? previous),
      {
        id: 'handler',
        pass: false,
        values: { error: String(error), snapshot: bridge.handlerSnapshot },
        threads: [],
      },
    ];
  }
}
