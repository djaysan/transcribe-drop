# Tuning summaries to what you're working on

The Channels tab writes a short AI summary for each video. If you tell it what
you're building, it also flags **why a video is relevant to you** ("Relevant
because…"). It does this by reading a plain-text file, `context.md`, on every run.

## 1. Generate your context file

Paste the prompt below into Claude Code (or any Claude chat), answer in a
sentence or two, and save the result as **`context.md`** in this folder
(`LVerbeeck/transcribe-drop/`).

---

> Write me a short context file (`context.md`) that a local app will read to
> decide which YouTube videos are worth my time. Keep it under 200 words, plain
> text, no markdown headings. Cover: what I do, the tools and stack I use, the
> projects I'm actively working on right now, the topics I want to keep up with,
> and anything I explicitly do **not** care about. Write it as terse notes, not
> prose — it's for a model to skim, not a human to read.
>
> Here's the raw material: **[describe yourself in a few sentences — role, stack,
> current projects, what you're trying to learn, what to ignore]**

---

## 2. Point the app at it (optional)

By default the app reads `context.md` in this folder. To use a file elsewhere,
open the **Channels tab → ⚙ Settings** and set the "Context file" path there
(the same panel where you paste your API key). Relative paths are resolved
against the app folder; both the key and the path are saved to a local,
gitignored `settings.json`.

Update `context.md` whenever your focus changes — the app re-reads it on every
summary, so new videos are always scored against your latest context. The app
works fine with no context file at all (you just get plain summaries).
