# Actual route transport host controls

Run `python3 Tests/AudioRoutes/run.py` when native compiler execution is available.
The bounded runner compiles the production `SystemAudioSession`, `AudioRoutes`,
Swift audio executor, C dispatch queue and C lifecycle observer. It selects the
iOS/import boundary on macOS and supplies inert AV values and setter methods;
the system AVFAudio framework is neither linked nor called.

Four groups cover a coalesced route/input notification burst, rejection of an
already blocked stale observation, actual-state rereading after a setter error,
and authorization changing false then true while input lookup is blocked. Every
facade session read/setter requires the real shared native queue. Blocked reads
have a four-second escape bound and signal entry through an `AsyncStream`; no
fixed yield counts or audio operations are used. Temporary products are removed.

These are host transport controls, not simulator/device route measurements.
