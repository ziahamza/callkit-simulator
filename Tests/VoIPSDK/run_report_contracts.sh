#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
TEST_OUTPUT=$(mktemp -d /tmp/ios-voip-report-contracts.XXXXXX)
trap 'rm -rf "$TEST_OUTPUT"' EXIT
python3 - "$TEST_OUTPUT" <<'PY'
from pathlib import Path
import sys
out = Path(sys.argv[1])
for filename in ('ConversationClient.swift', 'IncomingReportTransport.swift'):
    source = Path('Sources/VoIPSDK', filename).read_text()
    guard = '#if canImport(LiveCommunicationKit) && (os(iOS) || os(watchOS))'
    framework = '@preconcurrency import LiveCommunicationKit'
    assert source.count(guard) == source.count(framework) == 1
    source = source.replace(guard, '#if true', 1).replace(framework, '', 1)
    if filename == 'ConversationClient.swift':
        assert source.count('import AVFAudio') == 1
        source = source.replace('import AVFAudio', '', 1)
    (out / filename).write_text(source)
adapter = Path('Sources/VoIPSDK/MediaAdapter.swift').read_text()
adapter_guard = '#if canImport(AVFAudio) && (os(iOS) || os(watchOS))'
assert adapter.count(adapter_guard) == adapter.count('import AVFAudio') == 1
Path(sys.argv[1], 'MediaAdapter.swift').write_text(adapter.replace(adapter_guard, '#if true', 1).replace('import AVFAudio', '', 1))
PY
perl -e 'alarm shift; exec @ARGV' 30 xcrun clang -fobjc-arc -fmodules -c \
  Sources/VoIPPlatform/VSProcessSDKOwnership.m -o "$TEST_OUTPUT/ownership.o"
for TEST_CASE in ReportContractTests ConnectionContractTests; do
  perl -e 'alarm shift; exec @ARGV' 90 xcrun swiftc -swift-version 6 -warnings-as-errors -parse-as-library \
    -import-objc-header Sources/VoIPPlatform/include/VSProcessSDKOwnership.h \
    Sources/VoIPSDK/ConversationModels.swift Sources/VoIPSDK/ConversationRecord.swift \
    Sources/VoIPSDK/ConversationHandler.swift Sources/VoIPSDK/IncomingInvitationGate.swift \
    Sources/VoIPSDK/IncomingIntake.swift Sources/VoIPSDK/PushWire.swift \
    Sources/VoIPSDK/SimulatorSupport.swift Sources/VoIPSDK/ProcessSDKOwnership.swift \
    Sources/VoIPSDK/AudioActivation.swift Sources/VoIPSDK/AudioAdapterOperations.swift Sources/VoIPSDK/AudioSessionQueue.swift Sources/VoIPSDK/AudioRoutes.swift \
    "$TEST_OUTPUT/MediaAdapter.swift" \
    "$TEST_OUTPUT/ConversationClient.swift" "$TEST_OUTPUT/IncomingReportTransport.swift" \
    Tests/VoIPSDK/IncomingCachePlatform.swift "Tests/VoIPSDK/$TEST_CASE.swift" \
    "$TEST_OUTPUT/ownership.o" -o "$TEST_OUTPUT/$TEST_CASE"
  perl -e 'alarm shift; exec @ARGV' 20 "$TEST_OUTPUT/$TEST_CASE"
done
