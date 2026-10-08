#!/bin/zsh
set -euo pipefail

project_root="${0:A:h}"
cd "$project_root"
swift build -c release

app="$project_root/dist/Meeting Recorder.app"
contents="$app/Contents"
rm -rf "$app"
mkdir -p "$contents/MacOS" "$contents/Resources"
cp "$project_root/.build/release/MeetingRecorder" "$contents/MacOS/MeetingRecorder"
cp "$project_root/Sources/MeetingRecorder/Resources/process_session.py" "$contents/Resources/process_session.py"
cp "$project_root/Assets/AppIcon.icns" "$contents/Resources/AppIcon.icns"

cat > "$contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>MeetingRecorder</string>
  <key>CFBundleIdentifier</key><string>local.meeting-recorder</string>
  <key>CFBundleName</key><string>Meeting Recorder</string>
  <key>CFBundleDisplayName</key><string>Meeting Recorder</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.6</string>
  <key>CFBundleVersion</key><string>7</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSMicrophoneUsageDescription</key><string>会議の音声を録音して議事録を作成するためにマイクを使用します。</string>
  <key>NSScreenCaptureUsageDescription</key><string>Zoomなどのシステム音声を録音するために画面収録の権限を使用します。映像は保存しません。</string>
</dict></plist>
PLIST

signing_identity="$(security find-identity -v -p codesigning | awk '/Apple Development/{print $2; exit}')"
if [[ -n "$signing_identity" ]]; then
  codesign --force --deep --timestamp=none --sign "$signing_identity" "$app"
else
  codesign --force --deep --sign - "$app"
fi
print "$app"
