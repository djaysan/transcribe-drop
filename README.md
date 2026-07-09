<div align="center">
  <img src="media/icon.png" width="120" alt="Transcribe Drop">
  <h1>Transcribe Drop</h1>
  <p><b>Transcribe any video, or follow a YouTube channel and let AI tell you what's worth watching — all on your Mac.</b></p>
</div>

![Transcribe Drop](media/screenshot.png)

Transcription runs entirely on your machine (whisper.cpp) — no accounts, no per-minute charges, nothing leaves your Mac. The only optional online part is the AI summaries.

## What it does

- **Transcribe** any video or audio — paste a URL (YouTube, Facebook, …) or drop a file. Local, free, private.
- **Follow a YouTube channel** — it lists the latest videos, transcribes them (YouTube captions first, on-device Whisper as fallback), and shows them in a grid.
- **AI summaries** — click a video and Claude Haiku summarises it, tuned to a short "what I'm working on" context file, flagging what's **relevant to you** (the ✨). Summaries are generated on demand, so you only pay for what you actually open (~1¢ each, then cached).
- **Collections** — group pasted video/reel URLs (including Facebook reels) under any name you like.
- **Watch-later checklist** — star videos (☆) and check them off as you get through them.

## Install (macOS)

1. Download **`Transcribe-Drop-macOS.zip`** from the [latest release](https://github.com/djaysan/transcribe-drop/releases/latest) and unzip it.
2. In the **Transcribe Drop** folder, **right-click `Open Transcribe Drop.command` → Open** (just this once — macOS blocks *double*-clicking downloaded scripts, but right-click → Open works). It approves the app and launches it.
3. Drag **Transcribe Drop.app** to your Applications folder — done.

First launch installs the tools it needs and downloads the ~148 MB model automatically. Requires [Homebrew](https://brew.sh) (used to fetch `yt-dlp`, `ffmpeg`, `whisper-cpp`).

> It's an unsigned app (no paid Apple Developer account), so that one-time right-click → Open is macOS's approval step. Prefer to build from source? Grab **`Transcribe-Drop-vX.Y.Z.zip`** and run `./install.sh`.

## AI summaries (optional)

Open **⚙ Settings**, paste an [Anthropic API key](https://console.anthropic.com), and — optionally — pick a context file describing what you do (there's a built-in prompt to generate one with Claude). Without a key the app still lists and transcribes everything; it just skips the summaries. Everything except the summaries is free and local.

## Notes

- **Facebook:** individual reels work (paste them under ＋ Clips); following a whole FB profile isn't possible — its reels tab can't be listed.
- Your data (transcripts, settings, model) lives in `~/Library/Application Support/Transcribe Drop`, so the app is fully movable.
- Built with [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [whisper.cpp](https://github.com/ggerganov/whisper.cpp) + [ffmpeg](https://ffmpeg.org) (transcription), Claude Haiku (summaries), and [pywebview](https://pywebview.flowrl.com) (UI).

## Run from source

Prefer not to build the app? `./install.sh` does everything, or run it directly:

```sh
brew install yt-dlp ffmpeg whisper-cpp
pip install pywebview anthropic
python3 transcribe-app.py
```

## License

MIT — see [LICENSE](LICENSE).
