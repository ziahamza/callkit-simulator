# M1 TypeScript bridge

**Measured:** `npm run build`, `npm run typecheck`, the repository format check,
and `node --test Expo/tests/m1-runtime.test.cjs` pass for this TypeScript slice.
The ten offline controls exercise the real dependency-free store/dispatcher
with a controlled bridge. They cover stale hydration, immutable snapshot identity,
nullable token invalidation, hydration failure/recovery, handler settlement,
cancellation, replacement/redelivery, reentrant abort-listener replacement,
cancellation before invocation, and event
subscription cleanup. These are JavaScript measurements; native integration and
the M1 simulator journeys remain **proposed** until the combined milestone runs.

The public entry exports RFC 0001's state hooks, imperative snapshot, handler,
commands, reports, push-token hook and lifecycle-event hook. The M0 internal
`ios-voip-sdk/internal/bootstrap` entry remains unchanged. Audio-route APIs arrive
in M2. The event types include watchdog and dial-request signals, but those
producers arrive in M2 and M3 respectively; M1 can forward genuine activation,
deactivation and provider-reset signals.

RFC 0001 omits formal definitions for `Participant`, `IncomingCallPayload` and
`PendingAction`. The first two preserve the existing push fields. Pending actions
carry `id`, `kind`, ISO `deadline`, `origin` and the applicable `held`, `muted` or
`digits` argument. Conversation snapshots have no additional metadata field.
Incoming metadata stays in `payload`; outgoing metadata is passed to native.
Snapshot arrays and their nested bridge JSON are frozen copies. Applications
should treat state as read-only and use commands/reports to change it.

Fulfilling `join` or `start` confirms signaling acceptance. Incoming calls remain
`connecting` after the system joins; outgoing calls can remain `dialing` until a
`connecting` report. Neither path publishes `connected` or `connectedAt` until the
backend calls `reportConversation(id, { type: 'connected' })`. System hold and
termination states still take precedence over that transport progress.

`setConversationHandler(null)` removes the handler. Replacing or removing it
aborts the old handler's signals with `handlerReplaced` and ignores its late
promise results. Native retains unexpired actions, may redeliver them to the new
generation, and owns the deadline and unhandled-action policy. `deadline` in both
the pending snapshot and handler context is the effective application deadline:
initially 1.5 seconds before the native `timeoutDate`. The margin is an application
safety policy, not a measured platform interval. Native applies the policy there,
cancels running work, and refuses an accept if execution resumes after platform
expiry. Handler attachment does not subtract the margin again. JS never
uses a timer to extend or fulfill an expired action. Optional hold, mute and tones
callbacks accept when omitted. Handler callbacks may return `void` or a promise,
matching the RFC's synchronous mute example. Native filters system echoes.

`useConversation(id)` returns `undefined` when the system has no matching live
conversation. `usePushToken()` returns `null` before registration or after token
invalidation. The shared RFC token type includes `fcm`; this iOS module produces
`apns-voip`. `pushTokenUpdated` delivers that same nullable token, without the
private revision envelope. Reports return `Promise<void>` so native failures are
observable. No v1 API is retained.

`useAudioRoutes()` returns immutable `{ routes, current }` snapshots; `current`
is the actual OS output with an `output:<UID>` id, port name and raw port type,
or `null` when no output is observed. `input:<UID>` choices and `speaker` are
selection targets, not a complete output inventory or proof of receiver
availability. `selectAudioRoute(id)` requests an available input or speaker
override while system audio is active; selecting the already-current output id
is a no-op. Published state follows the subsequent OS read, including after a
failed request, rather than assuming that the requested route became current.

## Private native contract

The exact typed contract is `src/internal/protocol.ts`. All listeners attach
before the first snapshot request. Both `getConversations()` and
`conversationsChanged` carry `{ revision, conversations }`; both `getPushToken()`
and `pushTokenUpdated` carry `{ revision, token }`. Each stream has its own
nonnegative, monotonically increasing safe-integer revision. Older and equal
revisions do not replace the cache, so a delayed initial read cannot resurrect a
conversation or an invalidated token. A valid snapshot restores a failed initial
read. Hook initialization failures reach the React error boundary; imperative
read failures reject their promise.

`setHandlerAttached(attached, generation)` and
`settleAction(actionID, generation, success, error?)` are synchronous bridge
methods. Native serializes them with action delivery. `handlerAction` carries
`actionID`, `generation`, `kind`, `conversation`, `deadline`, `origin` and the
applicable action argument. `actionCancelled` carries `actionID`, `generation`
and a reason string. `error`, when present, is `{ name, message }`.

`conversationEvent` carries `{ type, payload }` for non-token public signals;
token events use their separate revisioned stream. The native module must detach
the old handler bridge on destruction, cancel its continuations, and discard
late settlements. Generations are scoped to that module/JS-context lifetime.
Within one JS context the runtime survives Fast Refresh under a global symbol,
so subscriptions, handler generations and pending signals are not duplicated.
Only one active JS context may own the process's native handler bridge.

These API details resolve gaps in the draft RFC and need review with the combined
M1 implementation. They do not claim device qualification, media integration or
default-calling entry-point support.
