# 🎙️ Transcribe Drop

A tiny macOS app that turns any video into text — **paste a link or drop a video, get a transcript.** Everything runs on your Mac: no accounts, no API keys, no per-minute charges.

## What it does

- **Paste a URL** (YouTube, Facebook, and most sites) → downloads just the audio and transcribes it
- **Drop a video on the app icon** (or click *Choose video…*) → transcribes a local file
- Pick the **language** (or auto-detect), and optionally **translate to English**
- Saves a clean `.txt` to the app's `output/` folder and shows it in the window (Copy / Reveal / Folder)

Under the hood: [yt-dlp](https://github.com/yt-dlp/yt-dlp) grabs the audio, [whisper.cpp](https://github.com/ggerganov/whisper.cpp) transcribes it locally, [ffmpeg](https://ffmpeg.org) does the audio conversion. The UI is [pywebview](https://pywebview.flowrl.com).

## Install

```sh
git clone https://github.com/djaysan/transcribe-drop.git
cd transcribe-drop
./install.sh
```

`install.sh` installs the tools it needs (`yt-dlp`, `ffmpeg`, `whisper-cpp` via Homebrew, plus `pywebview`) and builds **Transcribe Drop.app** in the folder. Drag it to your Dock.

Requirements: macOS + [Homebrew](https://brew.sh) + Python 3. The transcription model (~148 MB) downloads automatically the first time you transcribe something.

## Use it

- **Double-click** the app → paste a URL → **Transcribe**
- **Drop a video** onto the app's Dock icon → it opens preloaded, just hit **Transcribe**

## Notes

- **Languages:** uses the multilingual model (`ggml-base.bin`) — pick a language in the window or leave it on Auto-detect. Tick *Translate to English* to get an English transcript of a foreign-language video. For higher accuracy (slower), edit `MODEL` / `MODEL_URL` in `transcribe-app.py` to `ggml-small.bin` or `ggml-medium.bin` ([model list](https://huggingface.co/ggerganov/whisper.cpp/tree/main)).
- **Private / login-only videos** won't download — anything public works.
- **First launch:** macOS may ask permission for the app to launch itself; click Allow. If Gatekeeper blocks it, right-click → Open once.
- The 141 MB model is **not** in this repo (GitHub's file limit) — it's fetched on first run.

## License

MIT — see [LICENSE](LICENSE).
