#!/bin/sh
# install.sh — one-time setup for Transcribe Drop.
# Installs the CLI tools + Python packages it needs, then builds a self-contained
# "Transcribe Drop.app" you can drag to /Applications. (Tip: just double-click
# Install.command instead of running this in a terminal.)
set -e
cd "$(dirname "$0")"
# Use the SAME python the launched app will use (launch.sh puts Homebrew first),
# so the packages we install below are the ones the app finds — no slow first-run install.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
PY="$(command -v python3)"

echo "==> Checking command-line tools (yt-dlp, ffmpeg, whisper-cpp)…"
MISSING=""
for c in yt-dlp ffmpeg whisper-cli; do
  command -v "$c" >/dev/null 2>&1 || MISSING="$MISSING $c"
done
if [ -n "$MISSING" ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "    Installing:$MISSING"
    brew install yt-dlp ffmpeg whisper-cpp
  else
    echo "    Missing:$MISSING — install Homebrew (https://brew.sh) then re-run."
    exit 1
  fi
fi

echo "==> Setting up the app's Python environment (pywebview, anthropic)…"
# A dedicated venv keeps us off the externally-managed Homebrew Python (PEP 668)
# and guarantees the app finds these packages. Lives with the user data.
SUPPORT="$HOME/Library/Application Support/Transcribe Drop"
mkdir -p "$SUPPORT"
[ -x "$SUPPORT/venv/bin/python" ] || "$PY" -m venv "$SUPPORT/venv"
"$SUPPORT/venv/bin/pip" install --quiet --upgrade pip
# anthropic is only used for the optional AI summaries; the app runs without a key.
"$SUPPORT/venv/bin/pip" install --quiet pywebview anthropic

echo "==> Building a self-contained 'Transcribe Drop.app'…"
APP="Transcribe Drop.app"
# The applet finds its own launcher via 'path to resource', so the app has no
# hard-coded paths and can be moved anywhere (including /Applications).
cat > /tmp/td-droplet.applescript <<'EOF'
on run
  do shell script "/bin/bash " & quoted form of (POSIX path of (path to resource "launch.sh")) & " > /dev/null 2>&1 &"
end run
on open theFiles
  set f to POSIX path of item 1 of theFiles
  do shell script "/bin/bash " & quoted form of (POSIX path of (path to resource "launch.sh")) & " " & quoted form of f & " > /dev/null 2>&1 &"
end open
EOF
rm -rf "$APP"
osacompile -o "$APP" /tmp/td-droplet.applescript
RES="$APP/Contents/Resources"
# Bundle the code + launcher + icon inside the app.
cp transcribe-app.py channels.py meetings.py ui.html launch.sh "$RES/"
chmod +x "$RES/launch.sh"

# Meetings tab: compile the Swift recorder (needs Xcode's command-line tools).
if command -v swiftc >/dev/null 2>&1; then
  echo "==> Building the meeting recorder…"
  swiftc -O recorder.swift -o "$RES/recorder"
  # A tiny helper app that posts the notifications: they show whose app they come from,
  # so this gives them the Transcribe Drop name and icon. No Dock icon (LSUIElement).
  echo "==> Building the notifier…"
  NOTIFIER="$APP/Contents/Helpers/Notifier.app"
  mkdir -p "$NOTIFIER/Contents/MacOS" "$NOTIFIER/Contents/Resources"
  swiftc -O notifier.swift -o "$NOTIFIER/Contents/MacOS/notifier"
  cp icon/AppIcon.icns "$NOTIFIER/Contents/Resources/"
  cat > "$NOTIFIER/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.lverbeeck.transcribe-drop.notifier</string>
  <key>CFBundleName</key><string>Transcribe Drop</string>
  <key>CFBundleDisplayName</key><string>Transcribe Drop</string>
  <key>CFBundleExecutable</key><string>notifier</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST
else
  echo "    Skipping the meeting recorder: run 'xcode-select --install', then this script again."
fi
# macOS blocks the microphone for an app that doesn't say why it wants it. osacompile
# already adds a generic line ("This script needs…"), so replace it, or add it if missing.
MIC_WHY="Transcribe Drop records your side of a call to transcribe it on this Mac."
/usr/libexec/PlistBuddy -c "Set :NSMicrophoneUsageDescription '$MIC_WHY'" "$APP/Contents/Info.plist" 2>/dev/null ||
  /usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string '$MIC_WHY'" "$APP/Contents/Info.plist"
# osacompile gives the applet no bundle id, so macOS lists it as "droplet" and can't
# reset its permissions. Give it a real identity and name.
BUNDLE_ID="com.lverbeeck.transcribe-drop"
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string $BUNDLE_ID" \
  -c "Add :CFBundleDisplayName string 'Transcribe Drop'" \
  -c "Set :CFBundleName 'Transcribe Drop'" "$APP/Contents/Info.plist" 2>/dev/null || true
# osacompile makes an 'on open' applet a droplet, whose icon is droplet.icns —
# overwrite both so the custom icon shows regardless of CFBundleIconFile.
if [ -f icon/AppIcon.icns ]; then
  cp icon/AppIcon.icns "$RES/applet.icns"
  cp icon/AppIcon.icns "$RES/droplet.icns"
fi
touch "$APP" "$APP/Contents/Info.plist"
# Code-sign as the last step, after all files are in place.
SIGN_ID="Transcribe Drop Local Signing"   # made once by make-signing-cert.sh
if security find-identity -v -p codesigning | grep -q "\"$SIGN_ID\""; then
  # Same certificate every build → macOS keeps the recording permissions across rebuilds.
  echo "==> Signing with '$SIGN_ID'…"
  # The notifier first: an app inside the app is signed before the one around it.
  [ -d "$APP/Contents/Helpers/Notifier.app" ] && codesign --force --sign "$SIGN_ID" "$APP/Contents/Helpers/Notifier.app"
  codesign --force --deep --sign "$SIGN_ID" "$APP"
else
  [ -d "$APP/Contents/Helpers/Notifier.app" ] && codesign --force --sign - "$APP/Contents/Helpers/Notifier.app"
  # Ad-hoc: without any signature a downloaded copy reads as "damaged"; ad-hoc gives the
  # normal "unverified developer → Open Anyway" flow instead.
  codesign --force --deep --sign - "$APP" 2>/dev/null || true
  # But a rebuild changes the ad-hoc signature, so macOS no longer matches an earlier
  # recording permission to it and keeps asking even though it shows "enabled".
  # Clearing it means the next Record asks once, cleanly. (./make-signing-cert.sh avoids this.)
  tccutil reset ScreenCapture "$BUNDLE_ID" >/dev/null 2>&1 || true
  tccutil reset Microphone "$BUNDLE_ID" >/dev/null 2>&1 || true
fi

echo ""
echo "Done. Drag 'Transcribe Drop.app' into your Applications folder (or double-click it right here)."
echo "First time you open it: right-click → Open, then confirm (it's an unsigned local app)."
echo "The transcription model (~148 MB) downloads automatically the first time it's needed."
