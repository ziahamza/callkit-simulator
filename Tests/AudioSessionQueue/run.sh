#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
TEST_OUTPUT=$(mktemp -d "${TMPDIR:-/tmp}/ios-voip-audio-queue-tests.XXXXXX")
trap 'rm -rf "$TEST_OUTPUT"' EXIT
python3 - "$PWD" "$TEST_OUTPUT" <<'PY'
from pathlib import Path
import sys
root, out = map(Path, sys.argv[1:])
(out / 'module.modulemap').write_text('module VoIPPlatform { header "' +
    str(root / 'Sources/VoIPPlatform/include/VSAudioSessionQueue.h') + '" export * }\n')
PY
xcrun clang -target arm64-apple-macos14.0 -fobjc-arc -fmodules -Wall -Wextra -Werror \
  -c Sources/VoIPPlatform/VSAudioSessionQueue.m -o "$TEST_OUTPUT/Queue.o"
xcrun swiftc -swift-version 6 -warnings-as-errors -target arm64-apple-macos14.0 \
  -parse-as-library -I "$TEST_OUTPUT" Sources/VoIPSDK/AudioSessionQueue.swift \
  Tests/AudioSessionQueue/QueueTests.swift "$TEST_OUTPUT/Queue.o" \
  -framework Foundation -o "$TEST_OUTPUT/QueueTests"
python3 - "$TEST_OUTPUT/QueueTests" <<'PY'
import subprocess, sys
result = subprocess.run([sys.argv[1]], capture_output=True, text=True, timeout=15)
print(result.stdout, end='')
print(result.stderr, file=sys.stderr, end='')
if result.returncode or not result.stdout.startswith('passed:'):
    raise SystemExit(result.returncode or 1)
PY
swiftc -swift-version 6 -warnings-as-errors -parse-as-library \
  Sources/VoIPSDK/AudioAdapterOperations.swift Tests/AudioSessionQueue/OperationsTests.swift \
  -o "$TEST_OUTPUT/OperationsTests"
perl -e 'alarm shift; exec @ARGV' 15 "$TEST_OUTPUT/OperationsTests"
