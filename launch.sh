#!/bin/bash
# Launcher that lives INSIDE Transcribe Drop.app (Contents/Resources).
# Runs the bundled code from a dedicated virtualenv so it never touches the
# system/Homebrew Python (which is externally managed). The venv + all user data
# live in ~/Library/Application Support/Transcribe Drop.
cd "$(dirname "$0")"
# Finder/Dock-launched apps get a minimal PATH without Homebrew — add it so
# yt-dlp / ffmpeg / whisper-cli (and a modern python3) are found.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# First run: make sure the CLI tools are present (installed via Homebrew).
MISSING=""
for c in yt-dlp ffmpeg whisper-cli; do command -v "$c" >/dev/null 2>&1 || MISSING="$MISSING $c"; done
if [ -n "$MISSING" ]; then
  if command -v brew >/dev/null 2>&1; then
    osascript -e 'display notification "Installing tools (one time, a few minutes)…" with title "Transcribe Drop"' 2>/dev/null
    brew install yt-dlp ffmpeg whisper-cpp >/dev/null 2>&1
  else
    osascript -e 'display dialog "Transcribe Drop needs Homebrew to install its tools. Get it from https://brew.sh, then reopen the app." buttons {"OK"} with icon caution' 2>/dev/null
    exit 1
  fi
fi

SUPPORT="$HOME/Library/Application Support/Transcribe Drop"
PY="$SUPPORT/venv/bin/python"
if [ ! -x "$PY" ]; then
  # No venv yet (app opened without running Install.command first) — build it now.
  osascript -e 'display notification "Setting up (one time)…" with title "Transcribe Drop"' 2>/dev/null
  mkdir -p "$SUPPORT"
  python3 -m venv "$SUPPORT/venv" && "$SUPPORT/venv/bin/pip" install --quiet pywebview anthropic
  PY="$SUPPORT/venv/bin/python"
fi
exec "$PY" transcribe-app.py "$@"
