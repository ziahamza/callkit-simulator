import bridge, { StartupReceipt, ThreadSample } from './modules/expo2-spike';
import type { Result } from './demonstrations';

function check(value: unknown, message: string): asserts value {
  if (!value) throw new Error(message);
}
export async function startupBeforeReload(): Promise<StartupReceipt> {
  const end = performance.now() + 5000;
  while (!bridge.startupReceipt.firstReceiptLocked && performance.now() < end)
    await new Promise((resolve) => setTimeout(resolve, 5));
  const receipt = bridge.startupReceipt;
  check(
    receipt.firstReceiptLocked &&
      receipt.observedLoads === 1 &&
      receipt.delegateInstalled &&
      receipt.installedMs > 0 &&
      receipt.firstBundleStartMs > receipt.installedMs,
    'Cold delegate installation precedes native bundle-load START',
  );
  return receipt;
}
export async function runInterop(first: StartupReceipt): Promise<Result[]> {
  const results: Result[] = [],
    threads: ThreadSample[] = [];
  try {
    const probe = new bridge.PingProbe('shared-record');
    const payload = await new Promise<{ label: string }>((resolve, reject) => {
      const listener = probe.addListener('ping', (value) => {
        clearTimeout(deadline);
        listener.remove();
        threads.push(bridge.threadSample('shared-object-event'));
        resolve(value);
      });
      const deadline = setTimeout(() => {
        listener.remove();
        reject(new Error('SharedObject event deadline'));
      }, 2000);
      probe.ping();
    });
    check(payload.label === 'shared-record', 'Typed SharedObject event');
    check(
      (await bridge.rawAsync(async () => {
        await new Promise((resolve) => setTimeout(resolve, 10));
        return 7;
      })) === 7,
      'Raw async resolve',
    );
    let rejected = false;
    try {
      await bridge.rawAsync(async () => {
        throw new Error('bounded raw rejection');
      });
    } catch {
      rejected = true;
    }
    check(rejected, 'Raw async reject');
    const blocking = await bridge.rawBlocking(() => {
      threads.push(bridge.threadSample('blocking-js-body'));
      const end = performance.now() + 15;
      while (performance.now() < end) {
        /* bounded JS work */
      }
      return performance.now();
    });
    check(
      blocking.times[2] >= blocking.times[1] &&
        blocking.times[2] - blocking.times[0] >= 15 &&
        blocking.counts.join(',') === '1,1',
      'Value return blocks native until JS returns',
    );
    check(
      blocking.threads[0].threadID === blocking.threads[1].threadID &&
        blocking.threads.every((value) => !value.isOnJavaScriptThread),
      'Dedicated blocking caller',
    );
    threads.push(...blocking.threads);
    results.push({
      id: 'shared-object-callbacks',
      pass: true,
      values: {
        payload,
        rawAsyncResolvedAndRejected: true,
        blockingTimes: blocking.times,
        nativeResumeCounts: blocking.counts,
      },
      threads,
    });
  } catch (error) {
    results.push({ id: 'shared-object-callbacks', pass: false, values: { error: String(error) }, threads });
  }
  try {
    const current = bridge.startupReceipt;
    const cold = bridge.handlerSnapshot.cases.find(
      (value) => value.id === 'native-cold' && value.runtime === 'native-launch',
    );
    check(
      current.firstReceiptLocked &&
        current.delegateInstalled &&
        current.installedMs === first.installedMs &&
        current.firstBundleStartMs === first.firstBundleStartMs &&
        current.observedLoads >= 2,
      'Reload cannot replace the first cold receipt',
    );
    check(
      cold?.outcome === 'default-fail' &&
        cold.created === 0 &&
        cold.resumes === 0 &&
        cold.returnedMs < first.firstBundleStartMs,
      'Native default applies before initial JS load',
    );
    results.push({
      id: 'early-pushkit',
      pass: true,
      values: {
        first,
        current,
        nativeColdDefault: cold,
        beforeJSByMs: first.firstBundleStartMs - first.installedMs,
        deviceTimingClaimed: false,
      },
      threads: [],
    });
  } catch (error) {
    results.push({
      id: 'early-pushkit',
      pass: false,
      values: { error: String(error), current: bridge.startupReceipt },
      threads: [],
    });
  }
  return results;
}
