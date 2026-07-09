#!/usr/bin/env python3
"""Transcribe Drop — paste a URL or drop a video, get a transcript. All local, no API.

Pipeline: yt-dlp (URL → audio) or ffmpeg (file → audio) → whisper.cpp → text file.
GUI is pywebview (native window + ui.html). A video dropped on the .app icon is
passed as argv[1] and preloaded.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

import webview

APP_DIR = Path(__file__).resolve().parent
# Multilingual model (handles English, French, Polish, … via the language picker /
# auto-detect). Swap to ggml-small.bin for higher accuracy at the cost of speed.
MODEL = APP_DIR / 'models' / 'ggml-base.bin'
MODEL_URL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin'
OUTPUT_DIR = APP_DIR / 'output'
FILE_TYPES = ('Video/Audio (*.mp4;*.mov;*.mkv;*.webm;*.m4a;*.mp3;*.wav;*.aac;*.flac)',)

window = None


def have(cmd: str) -> bool:
    return subprocess.run(['/usr/bin/which', cmd], capture_output=True).returncode == 0


def safe_name(title: str) -> str:
    name = re.sub(r'[^\w .-]', '', title).strip()[:60]
    return name or 'transcript'


class Api:
    def __init__(self, preload=None):
        self._preload = preload

    # Path of a file dropped onto the .app icon (or None), read once by the UI.
    def get_preload(self):
        return self._preload

    # Native "Choose file" dialog — returns a real filesystem path.
    def pick_file(self):
        res = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=FILE_TYPES)
        return res[0] if res else None

    def open_output(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(['/usr/bin/open', str(OUTPUT_DIR)])
        return True

    def reveal(self, path):
        subprocess.run(['/usr/bin/open', '-R', path])
        return True

    # Kick off transcription on a worker thread; progress is pushed to the UI.
    # lang: '' = auto-detect, or a code like 'en' / 'fr' / 'pl'.
    # translate: also translate the result to English.
    def transcribe(self, source, lang='', translate=False):
        threading.Thread(target=self._run, args=(source, lang, translate), daemon=True).start()
        return True

    # --- UI bridge helpers ---
    def _emit(self, fn, payload):
        window.evaluate_js(f'window.{fn}({json.dumps(payload)})')

    def _status(self, msg):
        self._emit('onStatus', msg)

    def _run(self, source, lang='', translate=False):
        try:
            source = (source or '').strip()
            if not source:
                return self._emit('onFail', 'Paste a URL or choose a video first.')

            is_url = source.startswith(('http://', 'https://'))
            needed = ['ffmpeg', 'whisper-cli'] + (['yt-dlp'] if is_url else [])
            missing = [c for c in needed if not have(c)]
            if missing:
                pkgs = ' '.join('whisper-cpp' if m == 'whisper-cli' else m for m in missing)
                return self._emit('onFail', f"Missing: {', '.join(missing)}. Install with:  brew install {pkgs}")

            self._ensure_model()

            with tempfile.TemporaryDirectory() as tmp:
                wav = os.path.join(tmp, 'audio.wav')
                if is_url:
                    self._status('Downloading audio…')
                    meta = os.path.join(tmp, 'meta.txt')
                    subprocess.run(
                        ['yt-dlp', '--no-update', '-q', '-x', '--audio-format', 'wav',
                         '--postprocessor-args', '-ar 16000 -ac 1',
                         '-o', os.path.join(tmp, 'audio.%(ext)s'),
                         '--print-to-file', '%(title)s', meta, source],
                        check=True, capture_output=True, text=True)
                    title = Path(meta).read_text().strip() if os.path.exists(meta) else 'transcript'
                else:
                    if not os.path.exists(source):
                        return self._emit('onFail', 'That file no longer exists.')
                    self._status('Reading video…')
                    subprocess.run(['ffmpeg', '-y', '-i', source, '-ar', '16000', '-ac', '1', wav],
                                   check=True, capture_output=True, text=True)
                    title = Path(source).stem

                self._status('Transcribing on your Mac… (longer clips take a bit)')
                base = os.path.join(tmp, 'out')
                cmd = ['whisper-cli', '-m', str(MODEL), '-f', wav, '-otxt', '-of', base, '-np',
                       '-l', lang if lang else 'auto']
                if translate:
                    cmd.append('-tr')  # translate to English
                subprocess.run(cmd, check=True, capture_output=True, text=True)
                text = Path(base + '.txt').read_text().strip()

            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            out = OUTPUT_DIR / f'{safe_name(title)}.txt'
            n = 1
            while out.exists():
                out = OUTPUT_DIR / f'{safe_name(title)} ({n}).txt'
                n += 1
            out.write_text(text)

            self._emit('onDone', {'title': title, 'text': text, 'file': str(out)})
        except subprocess.CalledProcessError:
            self._emit('onFail', "Couldn't process that — check the link or file and try again. "
                                 "Private/login-only videos won't download.")
        except Exception as e:  # noqa: BLE001 — surface anything unexpected to the UI
            self._emit('onFail', f'Unexpected error: {e}')

    def _ensure_model(self):
        if MODEL.exists():
            return
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        self._status('Downloading the transcription model (one time, ~148 MB)…')
        urllib.request.urlretrieve(MODEL_URL, MODEL)


def main():
    global window
    preload = sys.argv[1] if len(sys.argv) > 1 else None
    api = Api(preload)
    window = webview.create_window(
        'Transcribe Drop', str(APP_DIR / 'ui.html'),
        js_api=api, width=680, height=640, min_size=(560, 560),
    )
    webview.start()


if __name__ == '__main__':
    main()
