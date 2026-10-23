#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
TEST_OUTPUT=$(mktemp -d "${TMPDIR:-/tmp}/ios-voip-audio-tests.XXXXXX")
trap 'rm -rf "$TEST_OUTPUT"' EXIT
swiftc -swift-version 6 -warnings-as-errors -parse-as-library \
  Sources/VoIPSDK/ConversationModels.swift Sources/VoIPSDK/AudioActivation.swift Sources/VoIPSDK/AudioRoutes.swift \
  Tests/AudioCore/AudioTests.swift -o "$TEST_OUTPUT/AudioTests"
python3 - "$TEST_OUTPUT/AudioTests" <<'PY'
import subprocess, sys
result = subprocess.run([sys.argv[1]], capture_output=True, text=True, timeout=25)
print(result.stdout, end='')
print(result.stderr, file=sys.stderr, end='')
if result.returncode or 'SWIFT TASK CONTINUATION MISUSE' in result.stderr:
    raise SystemExit(result.returncode or 1)
PY
