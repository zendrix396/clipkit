# clipkit

Compact AI-agent tooling for source gathering and media workflows.

This repo is built for fast, scriptable use by agents like Claude and Codex. It bundles three small CLIs:

- `youtube-cli` for YouTube search, video inspection, frame capture, downloads, and transcripts
- `image-search-cli` for Google Images capture and asset fetching
- `gemini-cli` for Gemini sign-in flows and image generation

The focus is practical source collection for downstream video-editing and Remotion pipelines, not a general-purpose app.

## What It Does

- Search YouTube and save screenshots plus structured result JSON
- Inspect videos at chosen timestamps and capture frames with caption/dialogue context
- Download video/audio/subtitles through `yt-dlp`
- Search Google Images and save the top result assets
- Drive Gemini image generation from a reusable browser profile

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

## Quick Start

```powershell
youtube-cli search "python browser automation" --limit 8
youtube-cli inspect --results youtube-cli-output\search-YYYYMMDD-HHMMSS\results.json --index 1 --positions 0.25 0.5 0.75
youtube-cli download "https://www.youtube.com/watch?v=VIDEO_ID" --out downloads
image-search-cli "blue glass cube product photo"
gemini-cli login --email "you@gmail.com" --profile-dir gemini-profile
gemini-cli image "a clean product render on a white desk" --profile-dir gemini-profile
```

## Repo Layout

- [`youtube_cli/`](youtube_cli/README.md) - YouTube search, inspection, and download CLI
- [`image_search_cli/`](image_search_cli/README.md) - Google Images search and asset saver
- [`gemini_cli/`](gemini_cli/README.md) - Gemini login and image generation CLI

## Outputs

Generated data is written to these folders:

- `youtube-cli-output/`
- `image-search-output/`
- `gemini-cli-output/`

These are ignored by git so the repo stays clean for publishing.

## Agent Notes

- Prefer visible browser mode if a profile needs login or verification.
- Keep outputs in timestamped folders for easy handoff between agents.
- Pass the JSON files and screenshots into the next agent step instead of re-scraping whenever possible.

## Naming

If you want a low-key casual project name, keep it short and plain. A few good options:

- `scrape-sources`
- `sourceflow`
- `agent-assets`
- `clipkit`

