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
cp transcribe-app.py channels.py ui.html launch.sh "$RES/"
chmod +x "$RES/launch.sh"
# osacompile makes an 'on open' applet a droplet, whose icon is droplet.icns —
# overwrite both so the custom icon shows regardless of CFBundleIconFile.
if [ -f icon/AppIcon.icns ]; then
  cp icon/AppIcon.icns "$RES/applet.icns"
  cp icon/AppIcon.icns "$RES/droplet.icns"
fi
touch "$APP" "$APP/Contents/Info.plist"
# Ad-hoc code-sign (sign as the last step, after all files are in place). Without
# any signature a downloaded copy reads as "damaged"; ad-hoc gives the normal
# "unverified developer → Open Anyway" flow instead.
codesign --force --deep --sign - "$APP" 2>/dev/null || true

echo ""
echo "Done. Drag 'Transcribe Drop.app' into your Applications folder (or double-click it right here)."
echo "First time you open it: right-click → Open, then confirm (it's an unsigned local app)."
echo "The transcription model (~148 MB) downloads automatically the first time it's needed."
