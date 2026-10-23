#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export CI=1 EXPO_NO_TELEMETRY=1
mkdir -p out
simulator_id=''
cleanup() {
  local cleanup_status=$?
  trap - EXIT
  if [[ -n "$simulator_id" ]]; then
    xcrun simctl shutdown "$simulator_id" >/dev/null 2>&1 || true
    xcrun simctl delete "$simulator_id" || cleanup_status=1
  fi
  node -e "require('node:fs').rmSync('out/DerivedData', {recursive: true, force: true, maxRetries: 3})" || cleanup_status=1
  exit "$cleanup_status"
}
trap cleanup EXIT

xcodebuild -version
xcrun swift --version
npx expo prebuild --platform ios --no-install
pod install --project-directory=ios >out/pods.log 2>&1
xcodebuild -workspace ios/Expo2Spike.xcworkspace -scheme Expo2Spike \
  -configuration Release -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath out/DerivedData ARCHS="$(uname -m)" ONLY_ACTIVE_ARCH=YES \
  CODE_SIGNING_ALLOWED=NO COMPILER_INDEX_STORE_ENABLE=NO build >out/build.log 2>&1
app_path="$PWD/out/DerivedData/Build/Products/Release-iphonesimulator/Expo2Spike.app"
test -s "$app_path/main.jsbundle"
simulator_id=$(xcrun simctl create "Expo2Spike-$$" \
  com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro \
  com.apple.CoreSimulator.SimRuntime.iOS-27-0)
xcrun simctl boot "$simulator_id"
xcrun simctl bootstatus "$simulator_id" -b
xcrun simctl ui "$simulator_id" appearance light
xcrun simctl install "$simulator_id" "$app_path"
xcrun simctl launch "$simulator_id" dev.iosvoipsdk.expo2spike
container_path=$(xcrun simctl get_app_container "$simulator_id" dev.iosvoipsdk.expo2spike data)
result_status=0
python3 scripts/check-results.py "$container_path/Documents/expo2-results.json" \
  --require snapshot change-event threading snapshot-benchmark actor-hazard handler shared-object-callbacks early-pushkit --timeout 60 || result_status=$?
xcrun simctl io "$simulator_id" screenshot out/scaffold.png || true
exit "$result_status"
