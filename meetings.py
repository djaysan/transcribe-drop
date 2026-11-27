#!/usr/bin/env python3
"""Meetings — record a call on this Mac, transcribe it locally, turn it into notes + tasks.

No bot joins the call. The Swift helper (recorder.swift) writes me.m4a (your mic) and
them.m4a (what the Mac plays: the other side) into one folder per meeting, on one shared
timeline. On stop, whisper transcribes both tracks mixed together, each word goes to Me or
Them by which track is louder while it's said (transcript.md), and Claude writes a
summary + task checklist (notes.md).

Auto-record: `recorder watch` reports once a second which apps hold the mic; AutoRecord
turns that into start/stop (the app runs the watcher and acts on it).

Video: the Brave/Chrome extension (extension/) records the call tab's picture and sends it
here in chunks (screen-<ms>.mp4); once the meeting is over, make_video() adds both audio
tracks → video.mp4.
"""
import base64
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

import channels  # shared data folder, settings, API key and context file

APP_DIR = Path(__file__).resolve().parent
RECORDER = APP_DIR / 'recorder'   # built from recorder.swift by install.sh
MEETINGS = channels.DATA_DIR / 'meetings'
FOLDER_FMT = '%Y-%m-%d %H-%M-%S'
# large-v3-turbo (5-bit, 574 MB) beats small on a French call: "case studies" not "cases
# te disent", "option B" not "baie". ~1.7x slower (~10 min per hour of call on an M1).
# VAD skips the silences (whisper invents lines in silence).
MODEL = channels.DATA_DIR / 'models' / 'ggml-large-v3-turbo-q5_0.bin'
MODEL_URL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin'
VAD = channels.DATA_DIR / 'models' / 'ggml-silero-v6.2.0.bin'
VAD_URL = 'https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin'
# whisper built by install.sh with Neural Engine (Core ML) support: the encoder runs on the
# Neural Engine instead of the graphics chip, 2x faster with under half the graphics-chip
# work, so far less fan. Homebrew's whisper-cli (graphics chip only) when it's missing.
WHISPER = channels.DATA_DIR / 'whisper' / 'whisper-cli'
# The Neural Engine half of MODEL. whisper finds it next to MODEL by name (-q5_0 dropped).
ENCODER = channels.DATA_DIR / 'models' / 'ggml-large-v3-turbo-encoder.mlmodelc'
ENCODER_URL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-encoder.mlmodelc.zip'
FRAME = 0.05   # seconds per loudness reading when telling Me from Them
NOTES_MODEL = 'claude-opus-5-5'
NO_SPEECH = '(No speech found in this recording.)'
START_AFTER = 20   # seconds an app must hold the mic before auto-record starts
STOP_AFTER = 15    # seconds of free mic before the call counts as over

# ponytail: one recording at a time, held in memory; the recorder exits with the app.
_rec = None   # (process, folder, start time) while recording


# ---- recording -----------------------------------------------------------

def start():
    """Start recording into a new meeting folder. Raises with a readable message on failure."""
    global _rec
    if _rec:
        return
    if not RECORDER.exists():
        raise RuntimeError("The meeting recorder isn't built. Install Xcode's command-line tools "
                           "(xcode-select --install), then run install.sh again.")
    d = MEETINGS / datetime.now().strftime(FOLDER_FMT)
    d.mkdir(parents=True)
    t0 = time.time()   # the recorder's audio starts at its launch: the video lines up with this
    # stderr goes to a file, not a pipe: nobody reads a pipe mid-call, and a full one stalls the recorder.
    with open(d / 'recorder.log', 'w') as log:
        p = subprocess.Popen([str(RECORDER), str(d)], stdout=subprocess.PIPE, stderr=log, text=True)
    if p.stdout.readline().strip() != 'recording':   # the helper prints this once capture runs
        p.wait()
        err = (d / 'recorder.log').read_text().strip()
        shutil.rmtree(d)
        raise RuntimeError(f"macOS didn't allow the recording ({err or 'no reason given'}). Allow Transcribe Drop "
                           'in System Settings → Privacy & Security → Screen & System Audio Recording and '
                           'Microphone, then quit and reopen the app.')
    _rec = (p, d, t0)


def stop():
    """Stop recording. Returns the meeting folder, or None if nothing was recording."""
    global _rec
    if not _rec:
        return None
    p, d, _ = _rec
    _rec = None
    p.send_signal(signal.SIGINT)   # the helper finalises both .m4a files, then exits
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
    return d


def state():
    """Whether a recording is running (and for how long), the last meeting language, and
    whether auto-record is on."""
    s = channels._load_settings()
    return {'recording': bool(_rec),
            'elapsed': time.time() - _rec[2] if _rec else 0,
            'lang': s.get('meeting_lang', ''),
            'auto': bool(s.get('auto_record'))}


def save_lang(lang):
    s = channels._load_settings()
    s['meeting_lang'] = lang or ''
    channels._save_settings(s)


def save_auto(on):
    s = channels._load_settings()
    s['auto_record'] = bool(on)
    channels._save_settings(s)


class AutoRecord:
    """Turns the watcher's once-a-second readings (which apps hold the mic) into 'start' or
    'stop'. A call = an app holding the mic START_AFTER seconds; it's over once the mic has
    been free STOP_AFTER seconds. Starts at most once per call, so a recording stopped by
    hand mid-call stays stopped, and only ever stops a recording it started itself, or one
    it was told to adopt (started from the browser extension) once the mic has been in use."""

    def __init__(self):
        self.busy_since = None   # when an app took the mic (None while it's free)
        self.free_since = None   # when the mic was released (None while it's held)
        self.done = False        # this call already has (or had) a recording
        self.ours = False        # the running recording was auto-started
        self.adopt = False       # treat the running recording as ours once an app holds the mic

    def tick(self, now, apps, recording, enabled):
        if not recording:
            self.ours = self.adopt = False   # stopped (by hand or by us): nothing of ours to stop
        if apps:
            self.free_since = None
            if self.busy_since is None:
                self.busy_since = now
            if recording:
                self.done = True  # recorded by hand: don't start a second one this call
                if self.adopt:
                    self.ours, self.adopt = True, False
            elif enabled and not self.done and now - self.busy_since >= START_AFTER:
                self.done = self.ours = True
                return 'start'
            return None
        self.busy_since = None
        if self.free_since is None:
            self.free_since = now
        if now - self.free_since >= STOP_AFTER:   # the call is over
            self.done = False
            if self.ours:
                self.ours = False
                return 'stop'
        return None


# ---- video (from the browser extension) ----------------------------------

_screens = {}   # a tab recording's start (ms, the extension's clock) → (its file, its meeting's start)
_video_lock = threading.Lock()   # make_video can be called from two threads for one meeting


def add_screen(start, data):
    """Append a chunk of the extension's tab recording (extension/offscreen.js)."""
    if start not in _screens:
        if not _rec:
            raise ValueError('No meeting is recording')
        _screens[start] = (_rec[1] / f'screen-{start}.mp4.part', _rec[2])
    with open(_screens[start][0], 'ab') as out:
        out.write(data)


def screen_done(start, first):
    """The tab recording ended. `first` (ms) is when its first frame arrived, the video's
    0:00: the file is renamed screen-<ms into the meeting>.mp4, which make_video lines up
    with the audio. Returns its meeting folder if that meeting is over, so the video can be
    made now; otherwise None, and stopping the meeting makes it."""
    f, t0 = _screens.pop(start, (None, 0))
    if not f or not f.exists():
        return None
    f.rename(f.parent / f'screen-{max(0, round(first - t0 * 1000))}.mp4')
    return None if _rec and _rec[1] == f.parent else f.parent


def make_video(d):
    """Each finished screen-<ms>.mp4 + both audio tracks → video.mp4 (video-2.mp4… if the
    video was stopped and started again). The picture starts <ms> in, so a time in the video
    is the same time in the transcript; before that it's sound only."""
    with _video_lock:
        shots = sorted(d.glob('screen-*.mp4'), key=lambda f: int(f.stem.split('-')[1]))
        for f in shots:
            n = len(list(d.glob('video*.mp4')))
            out = d / ('video.mp4' if n == 0 else f'video-{n + 1}.mp4')
            part = out.with_suffix('.part')
            subprocess.run([
                'ffmpeg', '-y', '-itsoffset', str(int(f.stem.split('-')[1]) / 1000), '-i', str(f),
                '-i', str(d / 'me.m4a'), '-i', str(d / 'them.m4a'),
                '-filter_complex', '[1:a][2:a]amix=inputs=2:duration=longest:normalize=0[a]',
                '-map', '0:v', '-map', '[a]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '48k',
                '-movflags', '+faststart', '-f', 'mp4', str(part)], check=True, capture_output=True)
            part.rename(out)
            f.unlink()


def videos(mid):
    return sorted(_folder(mid).glob('video*.mp4'))


def delete_video(mid):
    """Delete a meeting's video only; the audio, transcript and notes stay."""
    for f in videos(mid):
        f.unlink()
    (_folder(mid) / 'thumb.jpg').unlink(missing_ok=True)


def delete_meeting(mid):
    """Delete a whole meeting: audio, transcript, notes and video. Not the one recording."""
    d = _folder(mid)
    if _rec and _rec[1].resolve() == d:
        raise ValueError('Stop the recording before deleting it.')
    shutil.rmtree(d)


# ---- transcription -------------------------------------------------------

def _download(path, url, status):
    if path.exists():
        return
    status(f'Downloading {path.name} (one time)…')
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix('.part')   # never leave a half-downloaded model under the real name
    urllib.request.urlretrieve(url, part)
    part.rename(path)


def _ensure_encoder(status):
    """Fetch the Neural Engine model for the Core ML whisper build (one time, 1.2 GB zip).
    A failed download is fine: that build then runs on the graphics chip (slower, louder)."""
    if ENCODER.exists() or not WHISPER.exists():
        return
    zipped = ENCODER.parent / 'encoder.zip'
    unzipped = ENCODER.parent / 'encoder.unzip'   # never leave a half-extracted model under the real name
    try:
        _download(zipped, ENCODER_URL, status)
        status('Unpacking the Neural Engine model (one time)…')
        with zipfile.ZipFile(zipped) as z:   # the zip also holds a macOS __MACOSX folder: skip it
            z.extractall(unzipped, [n for n in z.namelist() if not n.startswith('__MACOSX')])
        (unzipped / ENCODER.name).rename(ENCODER)
    except (OSError, zipfile.BadZipFile):
        status('Neural Engine model not available: transcribing on the graphics chip.')
    finally:
        shutil.rmtree(unzipped, ignore_errors=True)
        zipped.unlink(missing_ok=True)


_models_lock = threading.Lock()


def prepare(status=lambda m: None):
    """Download the meeting models if missing (one time, ~1.8 GB). The app runs this in the
    background at launch, so the first meeting doesn't wait on it; process() runs it too, in
    case that one failed (offline). The lock makes a second caller wait, not download twice."""
    with _models_lock:
        _download(MODEL, MODEL_URL, status)
        _download(VAD, VAD_URL, status)
        _ensure_encoder(status)


def _whisper(tracks, lang):
    """Transcribe the tracks mixed into one. Returns [(start_seconds, end_seconds, word)]."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, 'a.wav')
        mix = ['-filter_complex', f'amix=inputs={len(tracks)}:normalize=0'] if len(tracks) > 1 else []
        subprocess.run(['ffmpeg', '-y', *[a for t in tracks for a in ('-i', str(t))], *mix,
                        '-ar', '16000', '-ac', '1', wav], check=True, capture_output=True)
        base = os.path.join(tmp, 'out')
        # Word-level output (-ml 1 -sow): with VAD on, whisper glues speech from either side
        # of a long pause into one segment, but each word keeps its real timestamp.
        cli = str(WHISPER) if WHISPER.exists() else 'whisper-cli'   # Neural Engine build first
        subprocess.run([cli, '-m', str(MODEL), '-f', wav, '-oj', '-of', base, '-np',
                        '-l', lang or 'auto', '--vad', '-vm', str(VAD), '-ml', '1', '-sow'],
                       check=True, capture_output=True)
        out = Path(base + '.json')
        if not out.exists():   # whisper writes nothing for a track with no audio (a 1-second recording)
            return []
        data = json.loads(out.read_text(errors='replace'))
    return [(s['offsets']['from'] / 1000, s['offsets']['to'] / 1000, s['text'])
            for s in data.get('transcription', [])]


def _loudness(track):
    """RMS level in dB of each FRAME-second slice of a track (ffmpeg astats)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, 'rms.txt')
        subprocess.run(['ffmpeg', '-i', str(track), '-af',
                        f'aresample=16000,asetnsamples=n={int(16000 * FRAME)}:p=0,astats=metadata=1:reset=1,'
                        f'ametadata=print:key=lavfi.astats.Overall.RMS_level:file={out}',
                        '-f', 'null', '-'], check=True, capture_output=True)
        levels = re.findall(r'RMS_level=(\S+)', Path(out).read_text())
    return [-120.0 if v == '-inf' else float(v) for v in levels]


def speakers(words, me, them, cap=0.6, quiet=-50.0, smooth=2):
    """Me/Them for each (start, end, word): whoever's track is louder during its first
    `cap` seconds. Without headphones the mic also hears the other side, but quieter than
    their own track. Each word then goes with the majority of the ±`smooth` words around
    it, so one misjudged word doesn't flip the speaker mid-sentence; bare punctuation and
    words said in silence (a stretched timestamp) don't vote, and a tie keeps the speaker
    before it."""
    def db(levels, a, b):
        xs = levels[int(a / FRAME):max(int(a / FRAME) + 1, int(b / FRAME))]
        return 10 * math.log10(sum(10 ** (x / 10) for x in xs) / len(xs)) if xs else -120.0

    votes = []
    for a, b, w in words:
        m, t = db(me, a, min(b, a + cap)), db(them, a, min(b, a + cap))
        heard = any(c.isalnum() for c in w) and max(m, t) > quiet
        votes.append(('Them' if t > m else 'Me') if heard else None)
    out, prev = [], 'Me'
    for i in range(len(votes)):
        near = votes[max(0, i - smooth):i + smooth + 1]
        mine, theirs = near.count('Me'), near.count('Them')
        if mine != theirs:
            prev = 'Me' if mine > theirs else 'Them'
        out.append(prev)
    return out


def phrases(words, pause=2.0):
    """[(start, word)] → [(start, text)], starting a new phrase when a word starts more
    than `pause` seconds after the previous one. Word START times only: the last word
    before a silence gets its end stretched across it. Punctuation always stays with the
    word before it."""
    out = []   # [phrase start, text, last word start]
    for start, w in words:
        if not w.strip():
            continue
        if out and (start - out[-1][2] < pause or not any(c.isalnum() for c in w)):
            out[-1][1] += w       # whisper words carry their own leading space
            out[-1][2] = start
        else:
            out.append([start, w.strip(), start])
    return [(start, text) for start, text, _ in out]


def merge(segs):
    """[(seconds, who, text)] → '[mm:ss] Who: text' paragraphs in time order, with
    consecutive lines from the same side joined into one paragraph."""
    out = []
    for t, who, text in sorted(segs):
        if out and out[-1][1] == who:
            out[-1][2] += ' ' + text
        else:
            out.append([t, who, text])
    return '\n\n'.join(f'[{int(t) // 60:02d}:{int(t) % 60:02d}] {who}: {text}' for t, who, text in out)


def process(mid, lang='', status=lambda m: None):
    """Transcribe both tracks into transcript.md, then write notes.md (if an API key is set).
    A meeting that already has a transcript is skipped. Returns the notes, or None."""
    d = _folder(mid)
    if (d / 'transcript.md').exists():
        return None
    prepare(status)
    # ponytail: "Them" is everyone on the other side; telling them apart needs a
    # diarization model (e.g. pyannote) if that ever matters.
    # One pass on both tracks mixed: transcribing each track alone put the other side's
    # voice (heard by the mic from the speakers) on both, and whisper's timing on a
    # mostly-silent track scattered a sentence's words over a minute.
    # A recording stopped before any audio arrived is a 28-byte header ffmpeg can't open (exit 183).
    tracks = {t: d / f'{t}.m4a' for t in ('me', 'them')
              if (d / f'{t}.m4a').exists() and (d / f'{t}.m4a').stat().st_size > 1000}
    segs = []
    if tracks:
        status('Transcribing (a few minutes per hour of call)…')
        words = _whisper(list(tracks.values()), lang)
        level = {t: _loudness(f) for t, f in tracks.items()}
        who = speakers(words, level.get('me', []), level.get('them', []))
        for side in ('Me', 'Them'):
            segs += [(t, side, text) for t, text in
                     phrases([(a, w) for (a, _, w), s in zip(words, who) if s == side])]
    # An empty result is still written, so the meeting doesn't retry on every launch.
    (d / 'transcript.md').write_text(merge(segs) or NO_SPEECH)
    return notes(mid, status) if segs else None


# ---- notes + tasks (Claude) ----------------------------------------------

def notes(mid, status=lambda m: None):
    """Ask Claude for a title, summary, decisions and tasks → notes.md. No key → skipped,
    the transcript is still there and notes are written when the meeting is next opened."""
    d = _folder(mid)
    key = channels._api_key()
    tp = d / 'transcript.md'
    if not key or not tp.exists() or tp.read_text() == NO_SPEECH:
        return None
    status('Writing notes and tasks…')

    import anthropic  # imported lazily so the app runs without the SDK/key
    system = ('You turn a recorded call into notes for the person who recorded it. Transcript lines are '
              'labelled "Me" (that person) or "Them" (everyone else on the call, possibly several people). '
              'The transcript is automatic speech recognition, so expect misheard words: infer the meaning, '
              'never invent facts. Write in English even if the call was in another language; keep names, '
              'products and figures as spoken. Plain markdown in exactly this shape, leaving out any '
              'section that would be empty:\n\n'
              '# <title, at most 8 words>\n\n'
              '## Summary\n<2-5 sentences>\n\n'
              '## Decisions\n- <one per line>\n\n'
              '## Tasks\n- [ ] <owner>: <task> (<deadline, if one was said>)\n\n'
              'Owner is "Me", a person\'s name if one was said, or "Them". Only list tasks someone actually '
              'agreed to or asked for.')
    ctx = channels.context_md()
    if ctx:
        system += f'\n\nAbout the person who recorded the call:\n{ctx}'

    msg = anthropic.Anthropic(api_key=key).beta.messages.create(
        model=NOTES_MODEL, max_tokens=16000,
        output_config={'effort': 'medium'},
        # If this model declines, the API re-runs the request on a fallback model.
        betas=['server-side-fallback-2026-07-01'], fallbacks='default',
        system=system,
        messages=[{'role': 'user', 'content': tp.read_text()}],
    )
    if msg.stop_reason == 'refusal':
        return None
    text = ''.join(b.text for b in msg.content if b.type == 'text').strip()
    (d / 'notes.md').write_text(text)
    info = _info(d)   # tokens shown on the meeting's tile
    info['tokens'] = msg.usage.input_tokens + msg.usage.output_tokens
    (d / 'info.json').write_text(json.dumps(info))
    return text


# ---- listing -------------------------------------------------------------

def _folder(mid):
    """The meeting folder for an id, refusing anything outside meetings/."""
    d = (MEETINGS / mid).resolve()
    if d.parent != MEETINGS.resolve() or not d.is_dir():
        raise ValueError('Unknown meeting')
    return d


def _title(d):
    n = d / 'notes.md'
    first = n.read_text().split('\n', 1)[0] if n.exists() else ''
    return first[2:].strip() if first.startswith('# ') else ''


def _when(name):
    try:
        return datetime.strptime(name, FOLDER_FMT).strftime('%a %d %b %Y, %H:%M')
    except ValueError:
        return name


def _info(d):
    """A finished meeting's length (seconds) and notes tokens, kept in info.json so the list
    doesn't re-read every meeting's audio."""
    f = d / 'info.json'
    info = json.loads(f.read_text()) if f.exists() else {}
    tracks = [d / f'{a}.m4a' for a in ('me', 'them') if (d / f'{a}.m4a').exists()]
    if 'length' not in info and tracks:
        outs = [subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0',
                                str(t)], capture_output=True, text=True).stdout for t in tracks]
        try:
            info['length'] = round(max(float(o) for o in outs))   # the longer track: one side can stop early
            f.write_text(json.dumps(info))
        except ValueError:
            pass   # not readable (yet): the recorder is still closing it → next time
    return info


def _thumb(d):
    """A frame from the middle of the meeting's video, as a data URL for its tile ('' if none).
    Made once into thumb.jpg."""
    # ponytail: thumbs ride in the list as data URLs (~20 KB each); serve files if the list gets long
    t, v = d / 'thumb.jpg', d / 'video.mp4'
    if not t.exists() and v.exists():
        out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v', '-show_entries',
                              'stream=start_time,duration', '-of', 'csv=p=0', str(v)],
                             capture_output=True, text=True).stdout
        try:
            start, dur = (float(x) for x in out.strip().split(',')[:2])
        except ValueError:
            return ''
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(start + dur / 2), '-i', str(v),
                        '-frames:v', '1', '-vf', 'scale=480:-2', '-q:v', '5', str(t)], capture_output=True)
    return 'data:image/jpeg;base64,' + base64.b64encode(t.read_bytes()).decode() if t.exists() else ''


def list_meetings():
    """Newest first. state: recording / pending (not transcribed yet) / done."""
    if not MEETINGS.exists():
        return []
    rec = _rec[1] if _rec else None
    out = []
    for d in sorted((p for p in MEETINGS.iterdir() if p.is_dir()), reverse=True):
        info = {} if d == rec else _info(d)
        out.append({'id': d.name, 'title': _title(d) or 'Meeting', 'when': _when(d.name),
                    'secs': info.get('length', 0), 'tokens': info.get('tokens', 0),
                    'thumb': '' if d == rec else _thumb(d),
                    'state': 'recording' if d == rec else
                             'done' if (d / 'transcript.md').exists() else 'pending'})
    return out


def pending():
    return [m['id'] for m in list_meetings() if m['state'] == 'pending']


def detail(mid):
    d = _folder(mid)
    read = lambda n: (d / n).read_text() if (d / n).exists() else ''
    return {'id': mid, 'title': _title(d) or _when(mid), 'when': _when(mid),
            'notes': read('notes.md'), 'transcript': read('transcript.md'), 'path': str(d),
            'video': any(d.glob('video*.mp4'))}


if __name__ == '__main__':
    # Self-check for phrase splitting + the Me/Them merge: python3 meetings.py
    ph = phrases([(0.0, ''), (0.05, ' Hi,'), (1.2, ' by'), (3.4, ' Wednesday'), (10.0, ','),
                  (14.0, ' yes'), (14.2, '.')])
    assert ph == [(0.05, 'Hi, by'), (3.4, 'Wednesday,'), (14.0, 'yes.')], ph
    # Speakers: 0-2 s I talk, 2-4 s they do and my mic hears them quieter (no headphones),
    # 4-6 s silence. A comma, a stretched word in the silence and one misjudged word
    # don't change who's talking.
    frames = int(2 / FRAME)
    me_db = [-20.0] * frames + [-35.0] * frames + [-70.0] * frames
    them_db = [-70.0] * frames + [-23.0] * frames + [-70.0] * frames
    words = [(0.1, 0.4, ' Bonjour'), (0.5, 0.9, ' Alex'), (1.0, 1.0, ','), (1.2, 1.5, ' ça'),
             (1.6, 1.9, ' va?'), (2.1, 2.5, ' Salut'), (2.6, 2.9, ' Ludo'), (3.0, 3.3, ' ouais'),
             (3.4, 3.7, ' top'), (4.5, 5.9, ' allez')]
    who = speakers(words, me_db, them_db)
    assert who == ['Me'] * 5 + ['Them'] * 5, who
    me_db[10:18] = [-70.0] * 8     # "Alex" (0.5-0.9 s) reads as quiet on my track
    them_db[10:18] = [-40.0] * 8   # and louder on theirs
    assert speakers(words, me_db, them_db) == who, speakers(words, me_db, them_db)
    assert speakers(words, me_db, []) == ['Me'] * 10   # no Them track
    out = merge([(65.0, 'Them', 'b'), (0.5, 'Me', 'a'), (1.2, 'Me', 'a2'), (70.0, 'Me', 'c')])
    assert out == '[00:00] Me: a a2\n\n[01:05] Them: b\n\n[01:10] Me: c', out

    # Auto-record. run() feeds one reading per second: (seconds, apps holding the mic).
    def run(auto, readings, recording=False, enabled=True):
        acts = []
        for t, apps in readings:
            act = auto.tick(t, apps, recording, enabled)
            recording = {'start': True, 'stop': False}.get(act, recording)
            acts += [(t, act)] if act else []
        return acts, recording
    call = [(t, ['Zoom']) for t in range(0, 31)] + [(t, []) for t in range(31, 50)]
    # 20 s of mic → start; 15 s after release → stop; the next call records again
    assert run(AutoRecord(), call + [(t + 100, a) for t, a in call])[0] == \
        [(20, 'start'), (46, 'stop'), (120, 'start'), (146, 'stop')]
    # a short release before 20 s restarts the count; off → never starts
    assert run(AutoRecord(), [(0, ['Zoom']), (10, []), (11, ['Zoom']), (30, ['Zoom']), (31, ['Zoom'])])[0] == [(31, 'start')]
    assert run(AutoRecord(), call, enabled=False)[0] == []
    # stopped by hand mid-call: no restart during that call
    a = AutoRecord()
    assert run(a, call[:21])[0] == [(20, 'start')]
    assert run(a, call[21:], recording=False)[0] == []
    # a recording started by hand is never auto-stopped
    assert run(AutoRecord(), call, recording=True) == ([], True)
    # adopted (started from the extension): stops after the call, but not before the mic is used
    a = AutoRecord()
    a.adopt = True
    assert run(a, [(t, []) for t in range(30)], recording=True) == ([], True)
    a.adopt = True
    assert run(a, call, recording=True)[0] == [(46, 'stop')]

    # Video chunks: named by how far into the meeting the first frame came; a recording that
    # ends while the meeting runs waits for the meeting, one that ends after it is made now.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _rec = (None, d, 1000.0)   # meeting started at t=1000 s
        add_screen(1000300, b'ab')
        add_screen(1000300, b'cd')
        assert (d / 'screen-1000300.mp4.part').read_bytes() == b'abcd'
        assert screen_done(1000300, 1012345) is None and (d / 'screen-12345.mp4').exists()
        add_screen(1020000, b'x')
        _rec = None
        assert screen_done(1020000, 1020000) == d and (d / 'screen-20000.mp4').exists()

        # Deleting: never the meeting that's recording
        MEETINGS = d
        (d / 'm1').mkdir()
        _rec = (None, d / 'm1', 0)
        try:
            delete_meeting('m1')
            raise AssertionError('deleted the recording')
        except ValueError:
            pass
        _rec = None
        delete_meeting('m1')
        assert not (d / 'm1').exists()
    print('ok')
