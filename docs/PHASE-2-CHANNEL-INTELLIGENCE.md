# Transcribe Drop — Phase 2: Channel Intelligence

Vision captured 2026-07-09 to hand off to a fresh chat. Phase 1 (the local one-off
transcriber app) is DONE and on GitHub (djaysan/transcribe-drop, v0.1.0). This doc
is the spec for the bigger "follow a channel, understand it, feed me what's worth
watching" product.

## What the user asked for

Paste a **channel / profile link** (e.g. a YouTube `@handle`, a Facebook profile
reels tab) and the app:
1. Lists the channel's videos.
2. Transcribes the **latest 20** by default; a **"get more history"** button pulls
   older ones on demand (don't auto-crawl the whole back catalogue — cost/time).
3. **Auto-transcribes new videos** as they appear.
4. Summarises each video with AI, understands what the channel is about, and surfaces
   **what's worth a look** based on the user's context/history.

## Dashboard UX (as described)

- **Followed channels** shown on the dashboard, each with a **platform icon** (YouTube /
  Facebook / etc.) to identify the source.
- **Click a channel** → grid of its transcribed videos: each = **thumbnail + title**, and
  an **"ℹ" hover** reveals the AI summary ("what this is about").
- A feed / "worth a look" ranking tuned to the user.

## The context-MD link (key idea)

The app should be **aware of what the user is building**. Mechanism the user wants:
- Claude Code generates a **context MD** (stack, current projects, workflow, interests)
  from a pasted prompt. The app is **pointed at that MD's path** and **reads it on every
  run**, so it stays current.
- The app uses that context to decide **what's relevant** and **what to do with** the
  transcripts + summaries (e.g. "this video is relevant to your LaunchViz SEO work").
- Deliverable includes: a **ready-to-paste prompt** the user gives Claude Code, which
  produces/updates that context MD.

## Phase X (later)

- **Purchased-course ingestion:** paste a course URL; if it's password-protected, the
  user enters **email + password** so the app can log in, access the course, and
  transcribe its videos. (Security: store creds carefully — Keychain, not plaintext.
  Respect each platform's ToS.)
- **"Create a quiz" magic button:** generate a quiz from what was learned in a
  video/course to test real comprehension (+ reward). Rationale: people watch long
  videos/podcasts and *think* they understood, but it went in one ear out the other —
  a quiz challenges + reinforces.

## Open architecture decision (REVISIT in the new chat)

User chose **"extend the local Mac app"** for platform. BUT two requested features —
**auto-transcribe new videos** and an **always-updating dashboard feed** — need
something running 24/7. A local Mac app only polls when the Mac is on and the app is
open. Options to resolve:
- **Local-only:** auto-check runs opportunistically when the app is open (simplest,
  matches "free/local", but "auto" is best-effort).
- **Hybrid:** local app for the free whisper transcription (uses their hardware) +
  a small always-on poller/dashboard online.
- **Online app** (EasyPanel, like Orbit/SalesSuite): true auto + live feed, but VPS CPU
  for whisper and it's a bigger build. User leaned local; flag the tradeoff early.

## Sources

User wants **YouTube + Facebook + others** (TikTok/Instagram/…). Reality check:
- **YouTube:** reliable listing + new-video detection via `yt-dlp --flat-playlist`.
- **Facebook / IG / TikTok:** flaky, gated, may need login, can break. Build
  YouTube-solid first, add others behind that with graceful failure.

## Cost note (the one non-free part)

Transcription (whisper.cpp) stays **free**. The **AI layer** (summaries, channel
"about", relevance ranking, quizzes) uses **Claude tokens** — Haiku is cheap
(fractions of a cent per transcript) but it's recurring. Summarising a big backlog
once = a few dollars. Keep the default at "latest 20" to control this.

## Building blocks that already exist (reuse)

- Phase 1 pipeline: `yt-dlp` (audio + metadata), `whisper.cpp` (`whisper-cli`), ffmpeg —
  see `transcribe-app.py`. `yt-dlp --flat-playlist <channel>` lists videos + thumbnails.
- User's stack for the online option: Next.js + Postgres + EasyPanel + cron workers
  (mirror Orbit/SalesSuite patterns), Claude Haiku for the AI layer.

## Suggested first step for the new chat

Scope a lean v1: follow 1 YouTube channel → list + transcribe latest 20 → per-video
AI summary → dashboard grid (thumb/title/ℹ-hover) → the context-MD prompt+link. Then
layer auto-new-video, "worth a look" ranking, Facebook/others, courses, quizzes.
