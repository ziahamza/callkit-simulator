#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
DIAL_TEST_OUTPUT=$(mktemp -d /tmp/ios-voip-dial-tests.XXXXXX)
trap 'rm -rf "$DIAL_TEST_OUTPUT"' EXIT
swiftc -swift-version 6 -warnings-as-errors -parse-as-library \
  Sources/VoIPSDK/DialRequest.swift Sources/VoIPSDK/DialRequest+Intents.swift Tests/DialRequests/DialRequestTests.swift \
  -o "$DIAL_TEST_OUTPUT/DialRequestTests"
"$DIAL_TEST_OUTPUT/DialRequestTests"
