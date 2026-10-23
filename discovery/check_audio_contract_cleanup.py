#!/usr/bin/env python3
"""Host-only timeout/cancellation cleanup control; native commands are shell stubs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).with_name('run_audio_lifecycle_legacy.sh')
OWNED = '11111111-1111-4111-8111-111111111111'
NAME = 'sdk-audio-duo-routes-host-synthetic-unique-name'


class CleanupControls(unittest.TestCase):
    def test_failed_or_cancelled_create_resolves_only_exact_owned_name(self):
        source = RUNNER.read_text()
        cleanup = source[source.index('cleanup_simulator() {'):source.index('redact_external_paths() {')]
        trap = source[source.index('cleanup() {'):source.index('python3 - "$SDK_M1_CONTRACT_BUILD"')]
        start = source.index('create_status=0\n')
        create = source[start:source.index('\nDEVICE_JSON=$(limit 60 xcrun simctl list devices -j)', start)]
        stub = r'''
limit() {
  printf '%s\n' "$*" >> "$OUT/commands.txt"
  case "$1 $2 $3 $4" in
    '30 xcrun simctl create')
      if [[ "$FIXTURE_MODE" == cancel-int ]]; then kill -INT "$$"; return 0; fi
      if [[ "$FIXTURE_MODE" == cancel-term ]]; then kill -TERM "$$"; return 0; fi
      return 142 ;;
    '60 xcrun simctl list')
      [[ "$FIXTURE_MODE" != query-fails ]] || return 124
      if [[ "$FIXTURE_MODE" == malformed ]]; then printf 'invalid JSON\n'; else cat "$OUT/inventory.json"; fi ;;
    '30 xcrun simctl shutdown') [[ "$5" == "$FIXTURE_OWNED" ]] ;;
    '30 xcrun simctl delete')
      [[ "$5" == "$FIXTURE_OWNED" ]] || return 99
      [[ "$FIXTURE_MODE" != delete-fails ]] ;;
    *) return 99 ;;
  esac
}
redact_external_paths() { :; }
'''
        for mode in ('owned', 'absent', 'query-fails', 'malformed', 'ambiguous', 'delete-fails', 'cancel-int', 'cancel-term'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='audio-contract-host-cleanup-') as temporary:
                out = Path(temporary)
                rows = [dict(name=NAME + '-unrelated', udid='another-qa'), dict(name='unrelated-qa', udid='other-qa')]
                if mode != 'absent': rows.append(dict(name=NAME, udid=OWNED))
                if mode == 'ambiguous': rows.append(dict(name=NAME, udid='22222222-2222-4222-8222-222222222222'))
                (out / 'inventory.json').write_text(json.dumps(dict(devices={'fixture-runtime': rows})))
                env = dict(os.environ, PATH=str(out) + ':/usr/bin:/bin', OUT=str(out),
                    FIXTURE_MODE=mode, FIXTURE_OWNED=OWNED, SIMULATOR_NAME=NAME,
                    DEVICE_TYPE='fixture-device-type', RUNTIME='fixture-runtime', MODE='duo-routes', RUN_ID='host-synthetic')
                (out / 'python3').symlink_to(sys.executable)
                harness = 'set -euo pipefail\nUDID=""\nCREATION_PENDING=0\n' + stub + cleanup + trap + create
                result = subprocess.run(['/bin/bash', '-c', harness], env=env, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                commands = (out / 'commands.txt').read_text().splitlines()
                self.assertTrue(commands[0].startswith('30 xcrun simctl create '))
                self.assertGreaterEqual(len(commands), 2, result.stderr)
                self.assertEqual(commands[1], '60 xcrun simctl list devices -j')
                if mode in ('owned', 'delete-fails', 'cancel-int', 'cancel-term'):
                    self.assertEqual(commands[2:], [f'30 xcrun simctl shutdown {OWNED}', f'30 xcrun simctl delete {OWNED}'])
                    status = json.loads((out / 'cleanup.json').read_text())
                    self.assertEqual(status['simulatorID'], OWNED)
                    self.assertIs(status['deleted'], mode != 'delete-fails')
                    expected = {'owned': 142, 'delete-fails': 1, 'cancel-int': 130, 'cancel-term': 143}
                    self.assertEqual(result.returncode, expected[mode])
                else:
                    self.assertEqual(commands[2:], [], result.stderr)
                    self.assertFalse((out / 'cleanup.json').exists())


if __name__ == '__main__':
    unittest.main()
