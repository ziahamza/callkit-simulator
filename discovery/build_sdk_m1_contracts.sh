#!/bin/bash
# Compile the Debug contract fixture from shared sources; never use a simulator.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:?provide a fresh output directory}
mkdir "$OUT"
OUT=$(cd "$OUT" && pwd)
limit() { perl -e 'alarm shift; exec @ARGV' "$@"; }
python3 - "$OUT/source-before.json" <<'PY'
import json,pathlib,sys
sys.path.insert(0,'discovery')
from sdk_m1_contracts_conformance import source_identity
pathlib.Path(sys.argv[1]).write_text(json.dumps(source_identity(),indent=2)+'\n')
PY
SDK=$(limit 30 xcrun --sdk iphonesimulator --show-sdk-path)
TARGET="${CKSIM_BUILD_ARCH:-$(uname -m)}-apple-ios17.4-simulator"
VERSION=$(cat VERSION)
WORK=$(mktemp -d "$OUT/.build.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
APP="$OUT/SDKM1Contracts.app"
mkdir -p "$APP/IosVoipSDKRuntime.bundle"
cp Sources/Engine/Resources/ios-27.0.json "$APP/IosVoipSDKRuntime.bundle/"
python3 - "$APP/Info.plist" <<'PY'
import pathlib,plistlib,sys
path=pathlib.Path(sys.argv[1])
info=plistlib.loads(pathlib.Path('discovery/probe/Info.plist').read_bytes())
info.update(CFBundleIdentifier='dev.iosvoipsdk.m1contracts',CFBundleExecutable='SDKM1Contracts',MinimumOSVersion='17.4')
path.write_bytes(plistlib.dumps(info))
(path.parent/'IosVoipSDKRuntime.bundle/Info.plist').write_bytes(plistlib.dumps(dict(
    CFBundleIdentifier='dev.iosvoipsdk.m1contracts.runtime',CFBundlePackageType='BNDL',CFBundleVersion='1')))
PY
cat > "$WORK/ContractHeaders.h" <<HEADERS
#import "$PWD/Sources/Interposition/CKRuntimeSeam.h"
#import "$PWD/Sources/VoIPPlatform/include/VoIPPlatform.h"
HEADERS
for source in Sources/Interposition/CKInterposition.m Sources/Interposition/CKPushDelivery.m Sources/Interposition/CKPushLaunch.m Sources/VoIPPlatform/*.m; do
  limit 90 xcrun --sdk iphonesimulator clang -target "$TARGET" -isysroot "$SDK" -fobjc-arc -fmodules \
    -DDEBUG=1 -DCKSIM_SDK_SOURCE=1 -DCKSIM_RUNTIME_VERSION=\""$VERSION"\" \
    -c "$source" -o "$WORK/$(basename "$source" .m).o" >> "$OUT/build.log" 2>&1
done
limit 180 xcrun --sdk iphonesimulator swiftc -target "$TARGET" -sdk "$SDK" -swift-version 6 -D DEBUG -Xcc -DDEBUG=1 -parse-as-library \
  -import-objc-header "$WORK/ContractHeaders.h" Sources/Engine/*.swift Sources/VoIPSDK/*.swift \
  discovery/probe/sdk_m1_contracts.swift "$WORK/"*.o -o "$APP/SDKM1Contracts" >> "$OUT/build.log" 2>&1
limit 30 codesign -s - --force "$APP" >> "$OUT/build.log" 2>&1
python3 - "$OUT" "$TARGET" <<'PY'
import pathlib,sys
sys.path.insert(0,'discovery')
from sdk_m1_contracts_conformance import record_build
record_build(pathlib.Path(sys.argv[1]),sys.argv[2])
PY
echo "Built $APP; no simulator was used."
