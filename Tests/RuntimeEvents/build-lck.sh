#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=${1:?provide a fresh output directory}
# An integration worktree can supply its Engine/Interposition root without
# copying or modifying sources in this fixture's isolated branch.
ENGINE_ROOT=${CKSIM_ENGINE_ROOT:-$PWD}
mkdir "$OUT"
OUT=$(cd "$OUT" && pwd)
trap 'rm -rf "$OUT/module-cache"; rm -f "$OUT/"*.o' EXIT
SDK=$(xcrun --sdk iphonesimulator --show-sdk-path)
TARGET="$(uname -m)-apple-ios18.0-simulator"
VERSION=$(cat "$ENGINE_ROOT/VERSION")
APP="$OUT/LCKRuntimeFixture.app"
mkdir "$APP"
for source in CKInterposition CKPushDelivery CKPushLaunch; do
  xcrun --sdk iphonesimulator clang -target "$TARGET" -isysroot "$SDK" -fobjc-arc -fmodules -fmodules-cache-path="$OUT/module-cache" \
    -DCKSIM_SDK_SOURCE=1 -DCKSIM_RUNTIME_VERSION=\""$VERSION"\" \
    -c "$ENGINE_ROOT/Sources/Interposition/$source.m" -o "$OUT/$source.o"
done
xcrun --sdk iphonesimulator swiftc -target "$TARGET" -sdk "$SDK" -swift-version 6 -parse-as-library \
  -module-cache-path "$OUT/module-cache" -DCKSIM_SDK_SOURCE -import-objc-header "$ENGINE_ROOT/Sources/Interposition/CKRuntimeSeam.h" \
  "$ENGINE_ROOT/Sources/Engine/RuntimeEngine.swift" Tests/RuntimeEvents/LCKFixture.swift \
  "$OUT/CKInterposition.o" "$OUT/CKPushDelivery.o" "$OUT/CKPushLaunch.o" -o "$APP/LCKRuntimeFixture"
python3 - "$APP/Info.plist" <<'PY'
import pathlib, plistlib, sys
info = plistlib.loads(pathlib.Path('discovery/probe/Info.plist').read_bytes())
info.update(CFBundleIdentifier='dev.callkitsim.lckfixture', CFBundleExecutable='LCKRuntimeFixture',
            CFBundleName='LCK Runtime Fixture', CFBundleDisplayName='LCK Fixture')
pathlib.Path(sys.argv[1]).write_bytes(plistlib.dumps(info))
PY
codesign -s - --force "$APP"
