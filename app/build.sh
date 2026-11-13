#!/bin/bash
# 构建 Wennec.app（原生外壳：托管 uvicorn 后端 + WebKit 窗口 + 菜单栏常驻）
# 用法: ./build.sh
set -e
cd "$(dirname "$0")"

ROOT="$(cd .. && pwd)"
APP="$ROOT/Wennec.app"
BUILD="$(pwd)/.build"

echo "▶ 生成图标…"
rm -rf "$BUILD"
mkdir -p "$BUILD"
swiftc -O -o "$BUILD/make_icon" Sources/make_icon.swift
"$BUILD/make_icon" "$BUILD/Fennec.iconset" >/dev/null
iconutil -c icns "$BUILD/Fennec.iconset" -o "$BUILD/Fennec.icns"

echo "▶ 编译应用…"
swiftc -O -o "$BUILD/Wennec" Sources/main.swift -framework AppKit -framework WebKit

echo "▶ 组装 $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Info.plist "$APP/Contents/Info.plist"
cp "$BUILD/Wennec" "$APP/Contents/MacOS/Wennec"
cp "$BUILD/Fennec.icns" "$APP/Contents/Resources/Fennec.icns"
codesign --force --deep --sign - "$APP" 2>/dev/null || true
touch "$APP"

echo "✅ 完成：$APP"
