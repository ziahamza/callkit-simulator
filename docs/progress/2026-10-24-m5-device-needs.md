# M5 device measurements needed — iPhone 13 / iOS 27.0

The runtime's multi-call group, capability and transaction model is simulator
policy. All multi-call timing and ordering remain **unverified** until measured.
LCK exposes individual action callbacks, without public group or transaction IDs;
correlate stable conversation UUIDs, and use a separate genuine CallKit probe
where transaction identity or composition must be observed.

| Initial state | Action | Record |
| --- | --- | --- |
| A joined; B incoming/idle | Hold & Accept | Pause A / Join B callback order and targets, pause value, deadlines, delivery versus fulfillment order, A paused and B joining/joined projections, audio deactivate/activate order |
| A joined; B incoming/idle | End & Accept | End A / Join B action inventory/order, A leaving/left/removal versus B joining/joined, independent settlement and audio ordering |
| A paused; B joined | Swap/resume A | Full system composition and pause values, transient states, which callbacks and audio events precede each other |
| A and B separate and joined | Merge in both target directions | Target semantics, any additional system pause/resume actions, resulting membership/states and audio changes |
| A and B grouped | Unmerge A; separate run unmerging B | Anchor/member split, surviving membership, paused states and audio order; also test singleton unmerge validity |
| A/B grouped; C separate | Move A into C's group | Whether only the selected conversation moves, B's remaining state, subsequent unmerge and audio behavior |
| A/B grouped and joined; C incoming | Hold & Accept; End & Accept | Whether the system acts on one or both grouped members, exact constituent inventory/order, partial state transitions and audio handoff |
| Configurations 1×1, 2×1, 1×2, 2×2 | Incoming, outgoing start, merge and unmerge at capacity | Which joined/paused/ringing/leaving states consume groups/members, exact completion domain/code, callbacks, and when end releases capacity |
| Ringing, joining, joined, paused and merged | Enable/disable `.pausing`, `.merging`, `.unmerging`, including updates during a call | System UI availability; direct application and system action outcomes; whose capabilities govern merge; when an update takes effect |
| Composite action delivered | Delay the first fulfillment, reverse fulfillment order, fail each constituent separately, leave each pending until expiry | Whether delivery waits for settlement, partial effects, rollback/cancellation, timeout and state/audio ordering |

For every run preserve OS/build, configuration, full stable UUIDs, monotonic and
wall timestamps, callback entry/exit, action parameters and deadlines,
fulfill/fail times, state and observed membership snapshots, and audio callbacks.
Keep simulator transaction IDs distinct from any genuine CallKit transaction IDs.

Incoming filters 4–7 also remain unverified. Where the device conditions are
available, capture the actual `com.apple.CallKit.error.incomingcall` domain/code,
report-entry-to-completion interval, process survival, and repeat behavior for
block list, restricted sharing mode, protected calls and sensitive participants.
Code 3's ≈2.07 s error, survival and 69-second repeated-caller filtering are
already documented in the [October 23 capture](../research/callkit-simulator-probe/livecommunicationkit/push/README.md).
