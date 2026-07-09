#!/bin/bash
# Double-click to allow + open Transcribe Drop.
# First time only: right-click this file → Open (macOS blocks double-clicking
# downloaded scripts, but right-click → Open works).
cd "$(dirname "$0")"
APP="Transcribe Drop.app"
if [ ! -d "$APP" ]; then
  echo "Couldn't find \"$APP\" next to this file. Keep them in the same folder."
  read -n 1 -s -r -p "Press any key to close…"; exit 1
fi
echo "Allowing Transcribe Drop to run (removing the download quarantine)…"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null
open "$APP" && echo "Opened. You can now drag Transcribe Drop.app to your Applications folder."
sleep 1
