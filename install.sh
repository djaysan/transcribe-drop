#!/bin/sh
# install.sh — one-time setup for Transcribe Drop.
# Installs the CLI tools + Python package it needs, then builds
# "Transcribe Drop.app" (an AppleScript droplet pointing at this folder).
set -e
cd "$(dirname "$0")"
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
    echo "    Missing:$MISSING — install Homebrew (https://brew.sh) then run:"
    echo "      brew install yt-dlp ffmpeg whisper-cpp"
    exit 1
  fi
fi

echo "==> Installing the Python UI package (pywebview)…"
"$PY" -m pip install --quiet pywebview

echo "==> Building 'Transcribe Drop.app'…"
cat > /tmp/transcribe-drop-droplet.applescript <<EOF
property py : "$PY"
property gui : "$(pwd)/transcribe-app.py"
on run
  do shell script py & " " & quoted form of gui & " > /dev/null 2>&1 &"
end run
on open theFiles
  set videoPath to POSIX path of item 1 of theFiles
  do shell script py & " " & quoted form of gui & " " & quoted form of videoPath & " > /dev/null 2>&1 &"
end open
EOF
rm -rf "Transcribe Drop.app"
osacompile -o "Transcribe Drop.app" /tmp/transcribe-drop-droplet.applescript
[ -f icon/applet.icns ] && cp icon/applet.icns "Transcribe Drop.app/Contents/Resources/applet.icns" || true
touch "Transcribe Drop.app"

echo ""
echo "Done. Double-click 'Transcribe Drop.app', or drag it to your Dock."
echo "Drop a video on its icon to transcribe that file; or open it and paste a URL."
echo "Transcripts are saved to the app's 'output' folder."
