#!/usr/bin/env python3
"""Channel intelligence — follow a YouTube channel, transcribe its videos, summarise each.

Data layer for Phase 2. Reuses the Phase 1 tools (yt-dlp, whisper-cli, ffmpeg).
Transcripts are captions-first (yt-dlp auto-subs, ~2s each) with a whisper fallback
for videos that have none. Summaries use Claude Haiku via the anthropic SDK, tuned by
a user context file (see docs/CONTEXT-PROMPT.md). No key set = no summaries, app still works.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# User data lives outside the app bundle so the .app can sit in /Applications
# (read-only) and still read/write. Older versions kept it next to the code;
# _migrate_data() moves that over on first run.
DATA_DIR = Path.home() / 'Library' / 'Application Support' / 'Transcribe Drop'
DB = DATA_DIR / 'data.db'
MODEL = DATA_DIR / 'models' / 'ggml-base.bin'  # whisper fallback model (shared with Phase 1)
MODEL_URL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin'
SETTINGS = DATA_DIR / 'settings.json'
MANUAL_CID = 'manual'   # the "Saved clips" pseudo-channel: pasted URLs, no auto-listing
LATEST = 20          # transcribe this many newest videos by default
HISTORY_BATCH = 20   # "get more history" pulls this many older ones
SUMMARY_MODEL = 'claude-haiku-4-5'   # cheap; the only paid part of the app
TRANSCRIPT_CAP = 120_000             # chars sent to the summariser (cost + context guard)


# ---- storage -------------------------------------------------------------

def _conn():
    # One connection per call — safe across the UI thread and the worker thread.
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c


def _migrate_data():
    """One-time move of user data from the old in-folder location to Application Support."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB.exists() or not (APP_DIR / 'data.db').exists():
        return
    for name in ('data.db', 'data.db-wal', 'data.db-shm', 'settings.json', '.env', 'context.md'):
        p = APP_DIR / name
        if p.exists():
            shutil.move(str(p), str(DATA_DIR / name))
    for d in ('models', 'output'):
        src = APP_DIR / d
        if src.exists() and not (DATA_DIR / d).exists():
            shutil.move(str(src), str(DATA_DIR / d))


def init_db():
    _migrate_data()
    with _conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS channels(
            id TEXT PRIMARY KEY, name TEXT, url TEXT, platform TEXT DEFAULT 'youtube');
        CREATE TABLE IF NOT EXISTS videos(
            id TEXT PRIMARY KEY, channel_id TEXT, title TEXT, duration INTEGER,
            thumb TEXT, position REAL, url TEXT, transcript TEXT, source TEXT, summary TEXT,
            starred INTEGER DEFAULT 0, watched INTEGER DEFAULT 0);
        ''')
        # Migrate older DBs that lack newer columns.
        cols = [r[1] for r in c.execute('PRAGMA table_info(videos)')]
        for col, ddl in (('url', 'url TEXT'),
                         ('starred', 'starred INTEGER DEFAULT 0'),
                         ('watched', 'watched INTEGER DEFAULT 0')):
            if col not in cols:
                c.execute(f'ALTER TABLE videos ADD COLUMN {ddl}')


def _ensure_model():
    """Make sure the whisper model is present (needed for clips with no captions)."""
    if MODEL.exists():
        return True
    try:
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL, MODEL)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- youtube listing -----------------------------------------------------

def _ytdlp(*args):
    return subprocess.run(['yt-dlp', '--no-update', *args],
                          check=True, capture_output=True, text=True)


def _list_videos(url, start=1, end=LATEST):
    """Flat-list a channel's videos (newest first). Returns lightweight dicts."""
    r = _ytdlp('--flat-playlist', '--playlist-start', str(start),
               '--playlist-end', str(end), '-J', url)
    data = json.loads(r.stdout)
    name = (data.get('channel') or data.get('uploader') or data.get('title') or url).strip()
    cid = data.get('channel_id') or data.get('id') or url
    vids = []
    for e in data.get('entries') or []:
        vid = e.get('id')
        if not vid:
            continue
        vids.append({
            'id': vid,
            'title': (e.get('title') or 'Untitled').strip(),
            'duration': int(e.get('duration') or 0),
            # Stable, unsigned thumbnail URL (the -J ones are signed/expiring).
            'thumb': f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg',
        })
    return cid, name, vids


def add_channel(url):
    """Follow a channel: store it + its latest videos (transcripts filled in later)."""
    cid, name, vids = _list_videos(url, 1, LATEST)
    with _conn() as c:
        c.execute('INSERT OR REPLACE INTO channels(id, name, url) VALUES (?,?,?)',
                  (cid, name, url))
        for i, v in enumerate(vids):
            c.execute('''INSERT OR IGNORE INTO videos(id, channel_id, title, duration, thumb, position)
                         VALUES (?,?,?,?,?,?)''',
                      (v['id'], cid, v['title'], v['duration'], v['thumb'], float(i)))
    return {'id': cid, 'name': name}


def refresh_channel(cid):
    """Check a followed channel for new videos; insert any above the current top. Returns count."""
    row = _get_channel(cid)
    if not row or row['platform'] == 'manual':   # "Saved clips" has nothing to auto-list
        return 0
    _, _, vids = _list_videos(row['url'], 1, LATEST)
    with _conn() as c:
        known = {r['id'] for r in c.execute('SELECT id FROM videos WHERE channel_id=?', (cid,))}
        top = c.execute('SELECT MIN(position) m FROM videos WHERE channel_id=?', (cid,)).fetchone()['m']
        top = top if top is not None else 0.0
        # yt-dlp returns newest first; insert reversed so the actual-newest sorts highest.
        new = [v for v in vids if v['id'] not in known]
        for offset, v in enumerate(reversed(new), start=1):
            c.execute('''INSERT OR IGNORE INTO videos(id, channel_id, title, duration, thumb, position)
                         VALUES (?,?,?,?,?,?)''',
                      (v['id'], cid, v['title'], v['duration'], v['thumb'], top - offset))
    return len(new)


def get_more_history(cid):
    """Pull the next batch of older videos on demand. Returns count added."""
    row = _get_channel(cid)
    if not row or row['platform'] == 'manual':
        return 0
    with _conn() as c:
        have = c.execute('SELECT COUNT(*) n FROM videos WHERE channel_id=?', (cid,)).fetchone()['n']
        bottom = c.execute('SELECT MAX(position) m FROM videos WHERE channel_id=?', (cid,)).fetchone()['m'] or 0.0
    _, _, vids = _list_videos(row['url'], have + 1, have + HISTORY_BATCH)
    with _conn() as c:
        known = {r['id'] for r in c.execute('SELECT id FROM videos WHERE channel_id=?', (cid,))}
        added = 0
        for v in vids:
            if v['id'] in known:
                continue
            added += 1
            c.execute('''INSERT OR IGNORE INTO videos(id, channel_id, title, duration, thumb, position)
                         VALUES (?,?,?,?,?,?)''',
                      (v['id'], cid, v['title'], v['duration'], v['thumb'], bottom + added))
    return added


def _probe(url):
    """Fetch a single video's metadata (title/duration/thumbnail) without downloading it."""
    try:
        r = _ytdlp('--skip-download', '-J', url)   # -J dumps JSON, downloads nothing
        d = json.loads(r.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    return {'title': (d.get('title') or url).strip(),
            'duration': int(d.get('duration') or 0),
            'thumb': d.get('thumbnail') or ''}


def _collection_id(name):
    """Stable id for a named collection. Default 'Saved clips' keeps the legacy 'manual' id."""
    name = (name or '').strip()
    if not name or name.lower() == 'saved clips':
        return MANUAL_CID
    return 'col_' + hashlib.sha1(name.lower().encode()).hexdigest()[:12]


def list_collections():
    """Names of existing clip collections (for the 'add to collection' dropdown)."""
    with _conn() as c:
        return [r['name'] for r in
                c.execute("SELECT name FROM channels WHERE platform='manual' ORDER BY name")]


def add_videos(urls, collection='Saved clips'):
    """Add pasted video/reel URLs (any yt-dlp source, incl. Facebook) to a named collection.
    Each becomes a card that gets transcribed + summarised like a channel video."""
    name = (collection or 'Saved clips').strip() or 'Saved clips'
    cid = _collection_id(name)
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO channels(id, name, url, platform) VALUES (?,?,?,?)",
                  (cid, name, '', 'manual'))
        top = c.execute('SELECT MIN(position) m FROM videos WHERE channel_id=?', (cid,)).fetchone()['m']
    top = top if top is not None else 0.0
    added = 0
    for url in urls:
        url = (url or '').strip()
        if not url:
            continue
        vid = 'm' + hashlib.sha1(url.encode()).hexdigest()[:14]   # stable id per URL → dedups re-pastes
        with _conn() as c:
            if c.execute('SELECT 1 FROM videos WHERE id=?', (vid,)).fetchone():
                continue
        meta = _probe(url) or {'title': url, 'duration': 0, 'thumb': ''}
        added += 1
        with _conn() as c:
            c.execute('''INSERT OR IGNORE INTO videos(id, channel_id, title, duration, thumb, position, url)
                         VALUES (?,?,?,?,?,?,?)''',
                      (vid, cid, meta['title'], meta['duration'], meta['thumb'], top - added, url))
    return {'id': cid, 'name': name, 'added': added}


def _get_channel(cid):
    with _conn() as c:
        return c.execute('SELECT * FROM channels WHERE id=?', (cid,)).fetchone()


def list_channels():
    with _conn() as c:
        out = []
        for ch in c.execute('SELECT * FROM channels ORDER BY name'):
            n = c.execute('SELECT COUNT(*) n FROM videos WHERE channel_id=?', (ch['id'],)).fetchone()['n']
            done = c.execute('SELECT COUNT(*) n FROM videos WHERE channel_id=? AND transcript IS NOT NULL',
                             (ch['id'],)).fetchone()['n']
            out.append({'id': ch['id'], 'name': ch['name'], 'platform': ch['platform'],
                        'total': n, 'done': done})
        return out


def channel_videos(cid):
    with _conn() as c:
        rows = c.execute('''SELECT id, title, duration, thumb, summary, starred,
                                   transcript IS NOT NULL AS done
                            FROM videos WHERE channel_id=? ORDER BY position''', (cid,))
        return [dict(r) for r in rows]


def get_video(vid):
    with _conn() as c:
        r = c.execute('''SELECT id, title, duration, thumb, summary, starred,
                                transcript IS NOT NULL AS done
                         FROM videos WHERE id=?''', (vid,)).fetchone()
        return dict(r) if r else None


def video_detail(vid):
    """Everything the click-to-open detail view needs (incl. full transcript + a watch URL)."""
    with _conn() as c:
        r = c.execute('''SELECT id, title, thumb, summary, transcript, url, starred, watched
                         FROM videos WHERE id=?''', (vid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d['url'] = d['url'] or f'https://www.youtube.com/watch?v={vid}'
    d['done'] = d['transcript'] is not None
    return d


# ---- starring / watch-later checklist ------------------------------------

def toggle_star(vid):
    """Flip a video's saved-for-later star. Returns the new state (0/1) or None."""
    with _conn() as c:
        cur = c.execute('SELECT starred FROM videos WHERE id=?', (vid,)).fetchone()
        if not cur:
            return None
        new = 0 if cur['starred'] else 1
        c.execute('UPDATE videos SET starred=? WHERE id=?', (new, vid))
    return new


def set_watched(vid, val):
    """Check/uncheck a starred video as watched (the checklist tick)."""
    with _conn() as c:
        c.execute('UPDATE videos SET watched=? WHERE id=?', (1 if val else 0, vid))
    return True


def starred_count():
    with _conn() as c:
        return c.execute('SELECT COUNT(*) n FROM videos WHERE starred=1').fetchone()['n']


def starred_videos():
    """The watch-later checklist: all starred videos, unwatched first, with their channel."""
    with _conn() as c:
        rows = c.execute('''SELECT v.id, v.title, v.thumb, v.url, v.watched,
                                   v.transcript IS NOT NULL AS done, c.name AS channel
                            FROM videos v JOIN channels c ON v.channel_id = c.id
                            WHERE v.starred = 1
                            ORDER BY v.watched, c.name, v.position''')
        out = []
        for r in rows:
            d = dict(r)
            d['url'] = d['url'] or f"https://www.youtube.com/watch?v={r['id']}"
            out.append(d)
        return out


def pending_videos():
    """Videos still missing a transcript (across all channels), newest first."""
    with _conn() as c:
        rows = c.execute('''SELECT id, channel_id FROM videos
                            WHERE transcript IS NULL ORDER BY channel_id, position''')
        return [dict(r) for r in rows]


# ---- transcription -------------------------------------------------------

def clean_vtt(text):
    """Auto-caption VTT → plain text. Strips headers, cue timings, and the inline
    <timing> tags, then collapses the rolling-window duplicate lines."""
    out, last = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('WEBVTT', 'Kind:', 'Language:')):
            continue
        if '-->' in line:            # cue timing line
            continue
        line = re.sub(r'<[^>]+>', '', line).strip()   # drop <00:00:00.200><c> …</c> tags
        if line and line != last:                     # collapse consecutive repeats
            out.append(line)
            last = line
    return '\n'.join(out).strip()


def _video_url(vid):
    """The URL to fetch. Manual clips store their own; YouTube videos rebuild the watch URL."""
    with _conn() as c:
        r = c.execute('SELECT url FROM videos WHERE id=?', (vid,)).fetchone()
    return (r['url'] if r and r['url'] else f'https://www.youtube.com/watch?v={vid}')


def _captions(url, tmp):
    """Try to grab English auto-captions. Returns cleaned text or None (e.g. Facebook has none)."""
    try:
        _ytdlp('--skip-download', '--write-auto-subs', '--sub-langs', 'en.*',
               '--sub-format', 'vtt', '-o', os.path.join(tmp, 'cap.%(ext)s'), url)
    except subprocess.CalledProcessError:
        return None
    for f in sorted(Path(tmp).glob('cap*.vtt')):   # en.vtt preferred over en-orig.vtt
        txt = clean_vtt(f.read_text())
        if txt:
            return txt
    return None


def _whisper(url, tmp):
    """Fallback: download the audio and run whisper-cli locally (free, slower). The only
    path for Facebook clips, which have no captions."""
    if not _ensure_model():
        return None
    wav = os.path.join(tmp, 'a.wav')
    subprocess.run(['yt-dlp', '--no-update', '-q', '-x', '--audio-format', 'wav',
                    '--postprocessor-args', '-ar 16000 -ac 1', '-o', wav.replace('.wav', '.%(ext)s'), url],
                   check=True, capture_output=True, text=True)
    base = os.path.join(tmp, 'out')
    subprocess.run(['whisper-cli', '-m', str(MODEL), '-f', wav, '-otxt', '-of', base, '-np', '-l', 'auto'],
                   check=True, capture_output=True, text=True)
    return Path(base + '.txt').read_text().strip()


def transcribe_video(vid):
    """Captions-first, whisper fallback. Stores the transcript. Returns (text, source)."""
    url = _video_url(vid)
    with tempfile.TemporaryDirectory() as tmp:
        text = _captions(url, tmp)
        source = 'captions'
        if not text:
            text = _whisper(url, tmp)
            source = 'whisper'
    if not text:
        return None, None
    with _conn() as c:
        c.execute('UPDATE videos SET transcript=?, source=? WHERE id=?', (text, source, vid))
    return text, source


# ---- AI summary ----------------------------------------------------------

# ---- settings (in-app, stored in gitignored settings.json) ---------------

def _load_settings():
    if SETTINGS.exists():
        try:
            return json.loads(SETTINGS.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_settings(d):
    SETTINGS.write_text(json.dumps(d, indent=2))


def save_api_key(key):
    d = _load_settings()
    d['api_key'] = (key or '').strip()
    _save_settings(d)


def set_context_path(path):
    d = _load_settings()
    d['context_md'] = (path or '').strip() or 'context.md'
    _save_settings(d)


def _context_path():
    p = _load_settings().get('context_md') or 'context.md'
    return Path(p) if os.path.isabs(p) else DATA_DIR / p


def settings_view():
    """What the Settings panel shows — never returns the key itself, only whether one is set."""
    ctx = _context_path()
    return {'has_key': bool(_api_key()),
            'context_md': _load_settings().get('context_md', 'context.md'),
            'context_exists': ctx.exists()}


def context_md():
    """User context the summariser is tuned to (path from settings.json, default context.md)."""
    p = _context_path()
    return p.read_text().strip() if p.exists() else ''


def _api_key():
    # Precedence: environment override → in-app settings.json → .env file.
    key = os.environ.get('ANTHROPIC_API_KEY')
    if key:
        return key
    key = _load_settings().get('api_key')
    if key:
        return key
    env = DATA_DIR / '.env'
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith('ANTHROPIC_API_KEY='):
                return line.split('=', 1)[1].strip().strip('"\'')
    return None


def has_summaries():
    return bool(_api_key())


def pending_summaries():
    """Videos already transcribed but not yet summarised (e.g. a key was just added)."""
    with _conn() as c:
        rows = c.execute('''SELECT id, channel_id FROM videos
                            WHERE transcript IS NOT NULL AND summary IS NULL
                            ORDER BY channel_id, position''')
        return [dict(r) for r in rows]


def summarize_video(vid):
    """Write a context-aware AI summary for a video. No key → skip. Returns summary or None."""
    key = _api_key()
    if not key:
        return None
    with _conn() as c:
        row = c.execute('SELECT title, transcript FROM videos WHERE id=?', (vid,)).fetchone()
    if not row or not row['transcript']:
        return None

    import anthropic  # imported lazily so the app runs without the SDK/key
    ctx = context_md()
    system = ('You summarise YouTube videos for one user so they can decide what is worth watching. '
              'Write 2-3 plain sentences on what the video covers. If it clearly connects to the '
              "user's work, add one final sentence starting \"Relevant because\". Otherwise omit it. "
              'No preamble, no markdown, no emoji.')
    if ctx:
        system += f'\n\nThe user is working on:\n{ctx}'

    msg = anthropic.Anthropic(api_key=key).messages.create(
        model=SUMMARY_MODEL, max_tokens=250,
        system=system,
        messages=[{'role': 'user',
                   'content': f"Video title: {row['title']}\n\nTranscript:\n{row['transcript'][:TRANSCRIPT_CAP]}"}],
    )
    summary = next((b.text for b in msg.content if b.type == 'text'), '').strip()
    with _conn() as c:
        c.execute('UPDATE videos SET summary=? WHERE id=?', (summary, vid))
    return summary
