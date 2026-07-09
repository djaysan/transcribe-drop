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

import channels  # Phase 2: channel intelligence (data layer)

APP_DIR = Path(__file__).resolve().parent
# User data (model, transcripts) lives in Application Support, not the app bundle —
# see channels.DATA_DIR. Multilingual model handles English/French/Polish/… ;
# swap to ggml-small.bin for higher accuracy at the cost of speed.
MODEL = channels.MODEL
MODEL_URL = channels.MODEL_URL
OUTPUT_DIR = channels.DATA_DIR / 'output'
# pywebview's filter description allows only word chars + spaces (no '/'), so keep it plain.
FILE_TYPES = ('Media files (*.mp4;*.mov;*.mkv;*.webm;*.m4a;*.mp3;*.wav;*.aac;*.flac)',)

window = None


def have(cmd: str) -> bool:
    return subprocess.run(['/usr/bin/which', cmd], capture_output=True).returncode == 0


def safe_name(title: str) -> str:
    name = re.sub(r'[^\w .-]', '', title).strip()[:60]
    return name or 'transcript'


class Api:
    def __init__(self, preload=None):
        self._preload = preload
        self._proc_lock = threading.Lock()  # serialises the channel-processing worker

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

    # --- Phase 2: channel intelligence -----------------------------------
    # Reads are synchronous (fast SQLite); anything hitting the network runs on a
    # worker thread and pushes results to the UI via onChannels / onVideos / etc.

    def get_channels(self):
        return channels.list_channels()

    def get_videos(self, cid):
        return channels.channel_videos(cid)

    def video_detail(self, vid):
        return channels.video_detail(vid)

    def open_url(self, url):
        # Open a video in the user's default browser (guarded to http/https).
        if url and url.startswith(('http://', 'https://')):
            subprocess.run(['/usr/bin/open', url])
        return True

    def has_ai(self):
        return channels.has_summaries()

    # --- settings (API key + context file), entered in the UI ---
    def get_settings(self):
        return channels.settings_view()

    def save_api_key(self, key):
        channels.save_api_key(key)
        return channels.settings_view()

    def save_context_path(self, path):
        channels.set_context_path(path)
        return channels.settings_view()

    # On-demand summary: generated the first time a video's detail is opened.
    def summarize_now(self, vid):
        try:
            channels.summarize_video(vid)
        except Exception:  # noqa: BLE001 — no key / API error → detail just shows no summary
            pass
        return channels.video_detail(vid)

    # Native "Choose file" dialog for the context .md — no path typing.
    def pick_context(self):
        res = window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=('Text files (*.md;*.markdown;*.txt)', 'All files (*.*)'))
        if res:
            channels.set_context_path(res[0])
        return channels.settings_view()

    def add_channel(self, url):
        threading.Thread(target=self._add_channel, args=(url,), daemon=True).start()
        return True

    def _add_channel(self, url):
        try:
            self._emit('onProc', 'Reading channel…')
            ch = channels.add_channel((url or '').strip())
            self._emit('onChannels', channels.list_channels())
            self._emit('onProc', f"Following {ch['name']} — fetching transcripts…")
            self._process()
        except subprocess.CalledProcessError:
            self._emit('onProc', "Couldn't read that channel — check the link (YouTube @handle or URL).")
        except Exception as e:  # noqa: BLE001
            self._emit('onProc', f'Error: {e}')

    def get_collections(self):
        return channels.list_collections()

    def add_videos(self, urls, collection='Saved clips'):
        threading.Thread(target=self._add_videos, args=(urls, collection), daemon=True).start()
        return True

    def _add_videos(self, urls, collection):
        try:
            self._emit('onProc', 'Reading clip details…')
            res = channels.add_videos(urls or [], collection)
            self._emit('onChannels', channels.list_channels())
            self._emit('onProc', f"Added {res['added']} clip(s) to {res['name']} — transcribing…")
            self._process()
        except Exception as e:  # noqa: BLE001
            self._emit('onProc', f'Error: {e}')

    # --- starring / watch-later checklist ---
    def get_starred(self):
        return channels.starred_videos()

    def toggle_star(self, vid):
        return channels.toggle_star(vid)

    def set_watched(self, vid, val):
        channels.set_watched(vid, val)
        return True

    def more_history(self, cid):
        threading.Thread(target=self._more_history, args=(cid,), daemon=True).start()
        return True

    def _more_history(self, cid):
        try:
            n = channels.get_more_history(cid)
            self._emit('onChannels', channels.list_channels())
            self._emit('onVideos', {'cid': cid, 'videos': channels.channel_videos(cid)})
            self._emit('onProc', f'Added {n} older videos — fetching transcripts…')
            self._process()
        except Exception as e:  # noqa: BLE001
            self._emit('onProc', f'Error: {e}')

    # Called once on launch: check followed channels for new uploads, then fill gaps.
    def refresh_channels(self):
        threading.Thread(target=self._refresh_all, daemon=True).start()
        return True

    def _refresh_all(self):
        try:
            if not channels.list_channels():
                return
            for ch in channels.list_channels():
                channels.refresh_channel(ch['id'])
            self._emit('onChannels', channels.list_channels())
            self._process()   # transcribe any new/pending videos (summaries are on-demand)
        except Exception:  # noqa: BLE001 — best-effort background refresh
            pass

    def _process(self):
        # Drain videos missing a transcript (captions-first). Summaries are generated
        # on demand when a video is opened — see summarize_now — to control token cost.
        with self._proc_lock:
            pending = channels.pending_videos()
            total = len(pending)
            for i, p in enumerate(pending, 1):
                self._emit('onProc', f'Transcribing {i} of {total}…')
                try:
                    text, _ = channels.transcribe_video(p['id'])
                except Exception:  # noqa: BLE001 — skip a bad video, keep going
                    continue
                if not text:
                    continue
                self._emit('onVideoDone', {'cid': p['channel_id'], 'video': channels.get_video(p['id'])})
            self._emit('onChannels', channels.list_channels())
            self._emit('onProc', 'Up to date.' if total else '')


def main():
    global window
    channels.init_db()
    preload = sys.argv[1] if len(sys.argv) > 1 else None
    api = Api(preload)
    window = webview.create_window(
        'Transcribe Drop', str(APP_DIR / 'ui.html'),
        js_api=api, width=860, height=720, min_size=(640, 600),
    )
    webview.start()


if __name__ == '__main__':
    main()
