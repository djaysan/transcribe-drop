#!/usr/bin/env python3
"""Transcribe Drop — paste a URL or drop a video, get a transcript. All local, no API.

Pipeline: yt-dlp (URL → audio) or ffmpeg (file → audio) → whisper.cpp → text file.
GUI is pywebview (native window + ui.html). A video dropped on the .app icon is
passed as argv[1] and preloaded.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import AppKit  # menu bar icon + Dock behaviour (installed with pywebview on macOS)
import objc
import webview
from PyObjCTools import AppHelper

import channels  # Phase 2: channel intelligence (data layer)
import meetings  # Meetings: record a call on this Mac → transcript → notes + tasks

APP_DIR = Path(__file__).resolve().parent
# User data (model, transcripts) lives in Application Support, not the app bundle —
# see channels.DATA_DIR. Multilingual model handles English/French/Polish/… ;
# swap to ggml-small.bin for higher accuracy at the cost of speed.
MODEL = channels.MODEL
MODEL_URL = channels.MODEL_URL
OUTPUT_DIR = channels.DATA_DIR / 'output'
# pywebview's filter description allows only word chars + spaces (no '/'), so keep it plain.
FILE_TYPES = ('Media files (*.mp4;*.mov;*.mkv;*.webm;*.m4a;*.mp3;*.wav;*.aac;*.flac)',)
# Posted by a second copy of the app, so the running one shows its window instead.
SHOW_NOTE = 'com.lverbeeck.transcribe-drop.show'

window = None
_lock = None   # held open while the app runs: see _already_running()


def have(cmd: str) -> bool:
    return subprocess.run(['/usr/bin/which', cmd], capture_output=True).returncode == 0


def safe_name(title: str) -> str:
    name = re.sub(r'[^\w .-]', '', title).strip()[:60]
    return name or 'transcript'


class Api:
    def __init__(self, preload=None):
        self._preload = preload
        self._proc_lock = threading.Lock()  # serialises the channel-processing worker
        self._meet_lock = threading.Lock()  # one meeting transcription at a time
        self.menubar = None                 # MenuBar, once the GUI loop is up

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

    # --- Meetings: record a call on this Mac → local transcript → notes + tasks ---
    # Starting/stopping and transcribing run on worker threads; results are pushed to
    # the UI via onMeetState / onMeetings / onMeetProc, and to the menu bar icon.

    def _meet_state(self):
        self._emit('onMeetState', meetings.state())
        if self.menubar:
            AppHelper.callAfter(self.menubar.refresh)

    def meeting_state(self):
        return meetings.state()

    def get_meetings(self):
        return meetings.list_meetings()

    def meeting_detail(self, mid):
        return meetings.detail(mid)

    def start_meeting(self):
        threading.Thread(target=self._start_meeting, daemon=True).start()
        return True

    def _start_meeting(self):
        try:
            self._emit('onMeetProc', 'Starting…')
            meetings.start()
            self._emit('onMeetProc', '')
        except Exception as e:  # noqa: BLE001 — e.g. recording permission denied → shown in the UI
            self._emit('onMeetProc', f'Error: {e}')
            _notify(str(e))   # the window may be hidden when recording from the menu bar
        self._meet_state()
        self._emit('onMeetings', meetings.list_meetings())

    def stop_meeting(self, lang=''):
        threading.Thread(target=self._stop_meeting, args=(lang,), daemon=True).start()
        return True

    def _stop_meeting(self, lang):
        meetings.save_lang(lang)
        meetings.stop()
        self._meet_state()
        self._process_meetings(lang)

    # Called when the Meetings tab opens: picks up meetings that never got transcribed
    # (e.g. the app was quit right after a call).
    def process_pending_meetings(self, lang=''):
        threading.Thread(target=self._process_meetings, args=(lang,), daemon=True).start()
        return True

    def _process_meetings(self, lang):
        with self._meet_lock:
            for mid in meetings.pending():   # read inside the lock, so nothing is done twice
                self._emit('onMeetings', meetings.list_meetings())
                try:
                    done = meetings.process(mid, lang, status=lambda m: self._emit('onMeetProc', m))
                    self._emit('onMeetProc', 'Notes ready.' if done else 'Transcript ready.')
                    _notify('Meeting notes ready.' if done else 'Meeting transcript ready.')
                except Exception as e:  # noqa: BLE001 — keep the audio; it retries next time the tab opens
                    self._emit('onMeetProc', f'Error: {e}')
                    _notify(f'Meeting error: {e}')
            self._emit('onMeetings', meetings.list_meetings())

    # On-demand notes: written the first time a transcribed meeting is opened
    # (e.g. the API key was added after the call).
    def meeting_notes_now(self, mid):
        try:
            meetings.notes(mid)
        except Exception:  # noqa: BLE001 — no key / API error → detail just shows no notes
            pass
        return meetings.detail(mid)

    # --- auto-record: start when a call app holds the mic, stop when it lets go ---
    def set_auto_record(self, on):
        meetings.save_auto(on)
        self._meet_state()   # keeps the Meetings tab and the menu bar checkmark in sync
        return meetings.state()

    def _watch_mic(self):
        """Runs for the app's lifetime on its own thread. The watcher always runs; the
        setting is checked on every reading, so switching it on mid-call works."""
        try:
            p = subprocess.Popen([str(meetings.RECORDER), 'watch'], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return   # recorder not built: auto-record never fires, manual recording says why
        auto = meetings.AutoRecord()
        for line in p.stdout:   # one reading a second, e.g. ["Brave Browser"]
            try:
                apps = json.loads(line)
            except ValueError:   # not a watcher: an older recorder build records into "./watch" instead
                p.terminate()
                return
            s = meetings.state()
            act = auto.tick(time.time(), apps, s['recording'], s['auto'])
            if act == 'start':
                self._start_meeting()   # on this thread, so the next reading sees it recording
                if meetings.state()['recording']:
                    _notify(f"Recording, mic in use by {', '.join(apps)}")
            elif act == 'stop':
                _notify('Call ended, transcribing…')
                self.stop_meeting(s['lang'])   # the language last picked in the Meetings tab


def _set_dock_icon():
    # The Dock shows the Python interpreter's rocket by default — point it at our
    # icon instead. Bundle: applet.icns in Resources; source: icon/AppIcon.icns.
    try:
        from AppKit import NSApplication, NSImage
        for cand in (APP_DIR / 'applet.icns', APP_DIR / 'icon' / 'AppIcon.icns'):
            if cand.exists():
                img = NSImage.alloc().initByReferencingFile_(str(cand))
                if img:
                    NSApplication.sharedApplication().setApplicationIconImage_(img)
                return
    except Exception:  # noqa: BLE001 — cosmetic only
        pass


def _already_running():
    """True if another copy of the app is running; that copy is asked to show its window.
    Opening the app while it sits in the menu bar would otherwise start a second copy, with
    a second mic watcher recording every call twice. The lock goes when the process ends.
    ponytail: a file dropped on the icon while the app runs only brings the window up."""
    global _lock
    _lock = open(channels.DATA_DIR / 'app.lock', 'w')
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        AppKit.NSDistributedNotificationCenter.defaultCenter() \
            .postNotificationName_object_userInfo_deliverImmediately_(SHOW_NOTE, None, None, True)
        return True


def _opened_at_login():
    """True when the app opens within 2 minutes of the Dock starting, i.e. as a Login Item
    (macOS doesn't tell an app it was opened that way). It then starts as the menu bar icon only.
    ponytail: a Dock restart (killall Dock) in the last 2 minutes also counts; the window is
    one click away in the menu bar icon either way."""
    out = subprocess.run(['/bin/ps', '-x', '-o', 'etime=,comm='], capture_output=True, text=True).stdout
    for line in out.splitlines():
        etime, _, comm = line.strip().partition(' ')
        if comm.endswith('/Dock'):
            parts = etime.split(':')   # mm:ss, then hh:mm:ss, then dd-hh:mm:ss
            return len(parts) == 2 and int(parts[0]) < 2
    return False


def _notify(msg):
    # macOS notification: the window may be hidden while a meeting runs from the menu bar.
    subprocess.run(['/usr/bin/osascript', '-e', 'on run argv', '-e',
                    'display notification (item 1 of argv) with title "Transcribe Drop"',
                    '-e', 'end run', msg])


def _show_window():
    AppHelper.callAfter(AppKit.NSApp.setActivationPolicy_, AppKit.NSApplicationActivationPolicyRegular)
    AppHelper.callAfter(_set_dock_icon)
    window.show()


def _on_closing():
    """The red button / Cmd+W only hide the window, so the menu bar icon keeps working
    (the Dock icon goes too, until it's reopened). Quit from the app menu, the Dock or
    the menu bar icon really quits; Cmd+Q never comes through here."""
    ev = AppKit.NSApp.currentEvent()
    if ev is None or ev.window() != window.native:   # not a click/key in our window → a real quit
        return True
    window.hide()
    AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    return False


class MenuBar(AppKit.NSObject):
    """Menu bar icon: start or stop a meeting recording without opening the window."""

    @objc.python_method
    def setup(self, api):
        self.api = api
        self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        menu = AppKit.NSMenu.alloc().init()
        self.rec = menu.addItemWithTitle_action_keyEquivalent_('Start recording', 'toggle:', '')
        self.auto = menu.addItemWithTitle_action_keyEquivalent_('Auto-record calls', 'auto:', '')
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_('Open Transcribe Drop', 'open:', '')
        menu.addItemWithTitle_action_keyEquivalent_('Quit Transcribe Drop', 'quit:', '')
        for it in menu.itemArray():
            it.setTarget_(self)
        self.item.setMenu_(menu)
        # A second copy of the app asks for the window (see _already_running).
        AppKit.NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, 'open:', SHOW_NOTE, None)
        self.refresh()
        return self

    @objc.python_method
    def refresh(self):
        s = meetings.state()
        on = s['recording']
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            'record.circle.fill' if on else 'waveform', 'Transcribe Drop')
        if on:
            # A red dot while recording: a coloured image, not a tint (tinting a menu bar
            # button breaks its light/dark drawing and leaves it black on a dark menu bar).
            img = img.imageWithSymbolConfiguration_(
                AppKit.NSImageSymbolConfiguration.configurationWithPaletteColors_([AppKit.NSColor.systemRedColor()]))
        img.setTemplate_(not on)   # template = macOS draws it white or black to match the menu bar
        self.item.button().setImage_(img)
        self.rec.setTitle_('Stop & transcribe' if on else 'Start recording')
        self.auto.setState_(AppKit.NSControlStateValueOn if s['auto'] else AppKit.NSControlStateValueOff)

    def toggle_(self, sender):
        s = meetings.state()
        if s['recording']:
            self.api.stop_meeting(s['lang'])   # the language last picked in the Meetings tab
        else:
            self.api.start_meeting()

    def auto_(self, sender):
        # Off the main thread: pushing the new state to the window from here would deadlock.
        threading.Thread(target=self.api.set_auto_record, args=(not meetings.state()['auto'],),
                         daemon=True).start()

    def open_(self, sender):
        _show_window()

    def quit_(self, sender):
        window.destroy()


def main():
    global window
    channels.init_db()
    if _already_running():
        return
    preload = sys.argv[1] if len(sys.argv) > 1 else None
    hidden = not preload and _opened_at_login()
    api = Api(preload)
    window = webview.create_window(
        'Transcribe Drop', str(APP_DIR / 'ui.html'),
        js_api=api, width=860, height=720, min_size=(640, 600), hidden=hidden,
    )
    window.events.closing += _on_closing
    _set_dock_icon()

    def add_menubar():
        api.menubar = MenuBar.alloc().init().setup(api)

    def ready():
        _set_dock_icon()                 # set again once the GUI loop is up (more reliable)
        AppHelper.callAfter(add_menubar)  # AppKit objects must be created on the main thread
        if hidden:   # menu bar icon only, like after closing the window
            AppHelper.callAfter(AppKit.NSApp.setActivationPolicy_, AppKit.NSApplicationActivationPolicyAccessory)
        threading.Thread(target=api._watch_mic, daemon=True).start()

    webview.start(ready)


if __name__ == '__main__':
    main()
