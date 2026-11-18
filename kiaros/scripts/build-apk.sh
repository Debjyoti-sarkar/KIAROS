#!/usr/bin/env bash
# Build the KIAROS Android app (debug APK, installable by sideloading).
set -euo pipefail
cd "$(dirname "$0")/../frontend"

export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
if [ -z "${JAVA_HOME:-}" ]; then
  for j in /usr/lib/jvm/java-21-openjdk-amd64 /usr/lib/jvm/java-17-openjdk-amd64; do
    [ -d "$j" ] && export JAVA_HOME="$j" && break
  done
fi
[ -d "$ANDROID_HOME" ] || { echo "Android SDK not found at $ANDROID_HOME"; exit 1; }

npm run build
npx cap sync android
( cd android && ./gradlew assembleDebug --no-daemon )

mkdir -p ../dist
cp android/app/build/outputs/apk/debug/app-debug.apk ../dist/KIAROS.apk
echo
echo "Built: $(cd .. && pwd)/dist/KIAROS.apk"
if command -v adb >/dev/null && adb get-state >/dev/null 2>&1; then
  read -r -p "A phone is connected over USB. Install now? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] && adb install -r ../dist/KIAROS.apk
else
  echo "Copy it to your phone and open it to install (allow 'Install unknown apps' for your file manager),"
  echo "or connect the phone with USB debugging and run: adb install -r dist/KIAROS.apk"
fi
