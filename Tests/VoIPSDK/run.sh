#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
TEST_OUTPUT=$(mktemp -d /tmp/ios-voip-core-tests.XXXXXX)
trap 'rm -rf "$TEST_OUTPUT"' EXIT
swiftc -swift-version 6 -warnings-as-errors -parse-as-library \
  Sources/VoIPSDK/ConversationModels.swift Sources/VoIPSDK/IncomingInvitationGate.swift \
  Sources/VoIPSDK/ConversationHandler.swift Sources/VoIPSDK/ConversationRecord.swift Tests/VoIPSDK/CoreTests.swift \
  -o "$TEST_OUTPUT/CoreTests"
"$TEST_OUTPUT/CoreTests"
swiftc -swift-version 6 -warnings-as-errors -parse-as-library \
  Sources/VoIPSDK/ConversationModels.swift Sources/VoIPSDK/ConversationHandler.swift \
  Tests/VoIPSDK/PreparationTests.swift -o "$TEST_OUTPUT/PreparationTests"
perl -e 'alarm shift; exec @ARGV' 15 "$TEST_OUTPUT/PreparationTests"
bash Tests/VoIPSDK/run_deadlines.sh
