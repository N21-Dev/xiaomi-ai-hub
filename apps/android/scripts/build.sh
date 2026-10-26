#!/usr/bin/env bash
set -euo pipefail

android_sdk_root="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
if [[ -z "$android_sdk_root" ]]; then
  echo "Set ANDROID_SDK_ROOT to your Android SDK directory." >&2
  exit 1
fi
android_build_tools="${ANDROID_BUILD_TOOLS_VERSION:-35.0.0}"
android_platform="${ANDROID_PLATFORM_VERSION:-34}"
android_tools="$android_sdk_root/build-tools/$android_build_tools"
android_jar="$android_sdk_root/platforms/android-$android_platform/android.jar"
android_root="$(cd "$(dirname "$0")/.." && pwd)"
android_build="$android_root/build"
android_out="$android_root/out"
android_mode="${1:---debug}"
if [[ "$android_mode" != --debug && "$android_mode" != --release ]]; then
  echo "Usage: build.sh [--debug|--release]" >&2
  exit 1
fi
for android_command in java javac keytool python3; do
  command -v "$android_command" >/dev/null || { echo "Missing: $android_command" >&2; exit 1; }
done
[[ -f "$android_jar" && -x "$android_tools/aapt2" ]] || { echo "Install the requested Android platform and build tools." >&2; exit 1; }
mkdir -p "$android_build" "$android_out"
# Only generated subdirectories inside this app's build directory are replaced.
python3 - "$android_build" <<'PY'
from pathlib import Path
import shutil,sys
root=Path(sys.argv[1])
for name in ['compiled','gen','classes','dex']:
    p=root/name
    if p.exists():shutil.rmtree(p)
    p.mkdir()
PY
"$android_tools/aapt2" compile --dir "$android_root/app/res" -o "$android_build/res.zip"
"$android_tools/aapt2" link -o "$android_build/unsigned.apk" -I "$android_jar" \
  --manifest "$android_root/app/AndroidManifest.xml" -R "$android_build/res.zip" \
  --java "$android_build/gen" --min-sdk-version 23 --target-sdk-version 34 --auto-add-overlay
python3 - "$android_root" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
paths=[*root.joinpath('app/src').rglob('*.java'),*root.joinpath('build/gen').rglob('*.java')]
(root/'build/sources.txt').write_text('\n'.join('"'+str(p)+'"' for p in paths)+'\n')
PY
javac -encoding UTF-8 --release 8 -classpath "$android_jar" -d "$android_build/classes" "@$android_build/sources.txt"
python3 - "$android_root" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
(root/'build/classes.txt').write_text('\n'.join('"'+str(p)+'"' for p in (root/'build/classes').rglob('*.class'))+'\n')
PY
java -cp "$android_tools/lib/d8.jar" com.android.tools.r8.D8 --min-api 23 --lib "$android_jar" --output "$android_build/dex" "@$android_build/classes.txt"
python3 "$android_root/scripts/add_dex.py" "$android_build/unsigned.apk" "$android_build/dex/classes.dex" "$android_build/with-dex.apk"
"$android_tools/zipalign" -f -p 4 "$android_build/with-dex.apk" "$android_build/aligned.apk"
if [[ "$android_mode" == --release ]]; then
  : "${ANDROID_KEYSTORE:?Set ANDROID_KEYSTORE to your private keystore}"
  : "${ANDROID_KEY_ALIAS:?Set ANDROID_KEY_ALIAS}"
  : "${ANDROID_KEYSTORE_PASSWORD:?Set ANDROID_KEYSTORE_PASSWORD}"
  android_keystore="$ANDROID_KEYSTORE"
  android_alias="$ANDROID_KEY_ALIAS"
else
  android_keystore="$android_build/debug.jks"
  android_alias=androiddebugkey
  export ANDROID_KEYSTORE_PASSWORD=android
  if [[ ! -f "$android_keystore" ]]; then
    keytool -genkeypair -keystore "$android_keystore" -storepass:env ANDROID_KEYSTORE_PASSWORD \
      -keypass:env ANDROID_KEYSTORE_PASSWORD -alias "$android_alias" -keyalg RSA -keysize 2048 \
      -validity 10950 -dname "CN=Android Debug,O=Android,C=US"
  fi
fi
android_apk="$android_out/ai-hub-${android_mode#--}.apk"
"$android_tools/apksigner" sign --ks "$android_keystore" --ks-key-alias "$android_alias" \
  --ks-pass env:ANDROID_KEYSTORE_PASSWORD --key-pass env:ANDROID_KEYSTORE_PASSWORD \
  --out "$android_apk" "$android_build/aligned.apk"
"$android_tools/apksigner" verify "$android_apk"
python3 - "$android_apk" <<'PY'
from pathlib import Path
import hashlib,json,sys
from xml.etree import ElementTree
p=Path(sys.argv[1])
manifest=ElementTree.parse(p.parent.parent/'app/AndroidManifest.xml').getroot()
ns='{http://schemas.android.com/apk/res/android}'
digest=hashlib.sha256(p.read_bytes()).hexdigest()
metadata={'versionCode':int(manifest.attrib[ns+'versionCode']),
          'versionName':manifest.attrib[ns+'versionName'],
          'file':p.name,'sha256':digest}
p.with_name('version.json').write_text(json.dumps(metadata,indent=2)+'\n')
print(p);print('SHA-256:',digest)
PY
