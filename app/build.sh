#!/bin/bash
# 原生录音 + WebKit 壳。FENNEC_BUILD_APP 可指定独立验收包；不覆盖正在运行的 App。
set -euo pipefail
cd "$(dirname "$0")"
PROJECT_ROOT="$(cd .. && pwd)"
TARGET_APP="${FENNEC_BUILD_APP:-$PROJECT_ROOT/Wennec.app}"
BUILD_DIR="$(pwd)/.build"
mkdir -p "$BUILD_DIR"
STAGING_DIR="$(mktemp -d "$BUILD_DIR/recorder-build.XXXXXX")"
STAGED_APP="$STAGING_DIR/Wennec.app"

if /bin/ps -axo comm= | /usr/bin/awk -v target="$TARGET_APP/Contents/MacOS/Wennec" '$0 == target {found=1} END {exit !found}'; then
  echo "请先退出正在运行的 Wennec 转写（会保存当前录音），再构建替换；或设置 FENNEC_BUILD_APP 生成独立验收包。" >&2
  exit 1
fi

echo "▶ 生成图标…"
xcrun swiftc -O -o "$STAGING_DIR/make_icon" Sources/make_icon.swift
"$STAGING_DIR/make_icon" "$STAGING_DIR/Wennec.iconset" >/dev/null
iconutil -c icns "$STAGING_DIR/Wennec.iconset" -o "$STAGING_DIR/Wennec.icns"

echo "▶ 编译原生录音与应用…"
xcrun swiftc -O -target "$(uname -m)-apple-macosx12.0" -o "$STAGING_DIR/Wennec" \
  Sources/main.swift Sources/RecordingController.swift Sources/NativeSpeechDetector.swift \
  -framework AppKit -framework WebKit -framework AVFoundation -framework SoundAnalysis
mkdir -p "$STAGED_APP/Contents/MacOS" "$STAGED_APP/Contents/Resources"
cp Info.plist "$STAGED_APP/Contents/Info.plist"
cp "$STAGING_DIR/Wennec" "$STAGED_APP/Contents/MacOS/Wennec"
cp "$STAGING_DIR/Wennec.icns" "$STAGED_APP/Contents/Resources/Wennec.icns"
plutil -lint "$STAGED_APP/Contents/Info.plist"

SIGN_IDENTITY="${CODE_SIGN_IDENTITY:-}"
if [ -z "$SIGN_IDENTITY" ]; then
  SIGN_IDENTITY="$(security find-identity -v -p codesigning | awk '/Apple Development:/ {print $2; exit}')"
fi
if [ -z "$SIGN_IDENTITY" ]; then
  SIGN_IDENTITY="-"
  echo "未找到开发签名，使用临时签名；后续重打包可能重新请求麦克风权限。"
fi
codesign --force --sign "$SIGN_IDENTITY" "$STAGED_APP"
codesign --verify --strict "$STAGED_APP"

# 编译过程中用户可能重新打开 App，替换前再次核对。
if /bin/ps -axo comm= | /usr/bin/awk -v target="$TARGET_APP/Contents/MacOS/Wennec" '$0 == target {found=1} END {exit !found}'; then
  echo "应用已打开，签好的验收包保留在 $STAGED_APP；请正常退出后再替换。" >&2
  exit 1
fi
mkdir -p "$(dirname "$TARGET_APP")"
if [ -e "$TARGET_APP" ]; then
  BACKUP_DIR="$PROJECT_ROOT/90_过程产物/应用构建备份/$(date +%Y%m%d-%H%M%S)-$$"
  mkdir -p "$BACKUP_DIR"
  mv "$TARGET_APP" "$BACKUP_DIR/$(basename "$TARGET_APP")"
  echo "原应用备份：$BACKUP_DIR"
fi
mv "$STAGED_APP" "$TARGET_APP"
cp "$STAGING_DIR/Wennec" "$BUILD_DIR/Wennec"
touch "$TARGET_APP"
echo "✅ 完成：$TARGET_APP"
