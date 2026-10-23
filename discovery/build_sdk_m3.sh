#!/bin/bash
# Build only the no-media example for direct URL/intent delivery. No simulator starts.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:?provide a fresh build directory}
mkdir "$OUT"
OUT=$(cd "$OUT" && pwd)
limit() { perl -e 'alarm shift; exec @ARGV' "$@"; }
python3 discovery/sdk_m0_conformance.py --begin-build "$OUT/source-before.json"
python3 - <<'PY'
import json,pathlib
assert json.loads(pathlib.Path('example/ios/Podfile.properties.json').read_text())['ios-voip-sdk.mediaAdapter']=='none'
PY
DD=${SDK_M3_DERIVED_DATA:-$OUT/expo}
SDK_EXAMPLE_MEDIA_ADAPTER=none limit 1800 xcodebuild \
  -workspace example/ios/VoIPSDKExample.xcworkspace -scheme VoIPSDKExample \
  -configuration Release -destination 'generic/platform=iOS Simulator' -derivedDataPath "$DD" \
  ARCHS="$(uname -m)" ONLY_ACTIVE_ARCH=YES CODE_SIGNING_ALLOWED=YES CODE_SIGN_IDENTITY=- \
  COMPILER_INDEX_STORE_ENABLE=NO build > "$OUT/example-build.log" 2>&1
cp -R "$DD/Build/Products/Release-iphonesimulator/VoIPSDKExample.app" "$OUT/Example.app"
python3 - "$OUT" <<'PY'
import json,pathlib,sys
sys.path.insert(0,'discovery')
import sdk_m0_conformance as m0
import sdk_m3_direct_conformance as m3
root=pathlib.Path(sys.argv[1]); identity=m0.source_identity()
assert json.loads((root/'source-before.json').read_text())==identity,'sources changed during M3 build'
app=root/'Example.app'
claims=m3.simulated_entitlements(m0.executable(app)); (root/'simulated-entitlements.plist').write_bytes(claims)
receipt=dict(schemaVersion=1,product='sdk-m3',configuration='Release',mediaAdapter='none',
             sourceStart=identity,sourceEnd=identity,**identity,
             files={'binaries/example':m0.digest(m0.executable(app)), 'binaries/main.jsbundle':m0.digest(app/'main.jsbundle'),'Info.plist':m0.digest(app/'Info.plist')},
             buildLogs={'example-build.log':m0.digest(root/'example-build.log')})
(root/'build-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
m3.check_input_build(app,receipt,identity)
PY
echo "Built source-bound M3 products at $OUT; no simulator was used."
