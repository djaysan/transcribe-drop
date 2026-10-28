#!/usr/bin/env python3
"""Meetings — record a call on this Mac, transcribe it locally, turn it into notes + tasks.

No bot joins the call. The Swift helper (recorder.swift) writes me.m4a (your mic) and
them.m4a (what the Mac plays: the other side) into one folder per meeting, on one shared
timeline. On stop, whisper transcribes each track, the segments are merged by time into
Me/Them lines (transcript.md), and Claude writes a summary + task checklist (notes.md).

Auto-record: `recorder watch` reports once a second which apps hold the mic; AutoRecord
turns that into start/stop (the app runs the watcher and acts on it).
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import channels  # shared data folder, settings, API key and context file

APP_DIR = Path(__file__).resolve().parent
RECORDER = APP_DIR / 'recorder'   # built from recorder.swift by install.sh
MEETINGS = channels.DATA_DIR / 'meetings'
FOLDER_FMT = '%Y-%m-%d %H-%M-%S'
# small is much better than base for French/Polish calls. VAD skips the long silences
# each track has while the other side talks (whisper invents lines in silence).
MODEL = channels.DATA_DIR / 'models' / 'ggml-small.bin'
MODEL_URL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin'
VAD = channels.DATA_DIR / 'models' / 'ggml-silero-v6.2.0.bin'
VAD_URL = 'https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin'
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
    _rec = (p, d, time.time())


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
    hand mid-call stays stopped, and only ever stops a recording it started itself."""

    def __init__(self):
        self.busy_since = None   # when an app took the mic (None while it's free)
        self.free_since = None   # when the mic was released (None while it's held)
        self.done = False        # this call already has (or had) a recording
        self.ours = False        # the running recording was auto-started

    def tick(self, now, apps, recording, enabled):
        if not recording:
            self.ours = False    # stopped (by hand or by us): nothing of ours to stop
        if apps:
            self.free_since = None
            if self.busy_since is None:
                self.busy_since = now
            if recording:
                self.done = True  # recorded by hand: don't start a second one this call
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


# ---- transcription -------------------------------------------------------

def _download(path, url, status):
    if path.exists():
        return
    status(f'Downloading {path.name} (one time)…')
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix('.part')   # never leave a half-downloaded model under the real name
    urllib.request.urlretrieve(url, part)
    part.rename(path)


def _whisper(audio, lang):
    """Transcribe one track. Returns [(start_seconds, text)] phrases."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, 'a.wav')
        subprocess.run(['ffmpeg', '-y', '-i', str(audio), '-ar', '16000', '-ac', '1', wav],
                       check=True, capture_output=True)
        base = os.path.join(tmp, 'out')
        # Word-level output (-ml 1 -sow): with VAD on, whisper glues speech from either side
        # of a long pause into one segment, but each word keeps its real timestamp.
        subprocess.run(['whisper-cli', '-m', str(MODEL), '-f', wav, '-oj', '-of', base, '-np',
                        '-l', lang or 'auto', '--vad', '-vm', str(VAD), '-ml', '1', '-sow'],
                       check=True, capture_output=True)
        out = Path(base + '.json')
        if not out.exists():   # whisper writes nothing for a track with no audio (a 1-second recording)
            return []
        data = json.loads(out.read_text(errors='replace'))
    return phrases([(s['offsets']['from'] / 1000, s['text']) for s in data.get('transcription', [])])


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
    _download(MODEL, MODEL_URL, status)
    _download(VAD, VAD_URL, status)
    # ponytail: "Them" is everyone on the other side; telling them apart needs a
    # diarization model (e.g. pyannote) if that ever matters.
    segs = []
    for track, who, label in (('me', 'Me', 'your side'), ('them', 'Them', 'the other side')):
        f = d / f'{track}.m4a'
        if f.exists() and f.stat().st_size:
            status(f'Transcribing {label} (a few minutes per hour of call)…')
            segs += [(t, who, text) for t, text in _whisper(f, lang)]
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


def list_meetings():
    """Newest first. state: recording / pending (not transcribed yet) / done."""
    if not MEETINGS.exists():
        return []
    rec = _rec[1] if _rec else None
    out = []
    for d in sorted((p for p in MEETINGS.iterdir() if p.is_dir()), reverse=True):
        out.append({'id': d.name, 'title': _title(d) or 'Meeting', 'when': _when(d.name),
                    'state': 'recording' if d == rec else
                             'done' if (d / 'transcript.md').exists() else 'pending'})
    return out


def pending():
    return [m['id'] for m in list_meetings() if m['state'] == 'pending']


def detail(mid):
    d = _folder(mid)
    read = lambda n: (d / n).read_text() if (d / n).exists() else ''
    return {'id': mid, 'title': _title(d) or _when(mid), 'when': _when(mid),
            'notes': read('notes.md'), 'transcript': read('transcript.md'), 'path': str(d)}


if __name__ == '__main__':
    # Self-check for phrase splitting + the Me/Them merge: python3 meetings.py
    ph = phrases([(0.0, ''), (0.05, ' Hi,'), (1.2, ' by'), (3.4, ' Wednesday'), (10.0, ','),
                  (14.0, ' yes'), (14.2, '.')])
    assert ph == [(0.05, 'Hi, by'), (3.4, 'Wednesday,'), (14.0, 'yes.')], ph
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
    print('ok')
