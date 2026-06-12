# youtube_cli

YouTube browser workflow for agents.

## Commands

```powershell
youtube-cli search "query" --limit 8
youtube-cli view "https://www.youtube.com/watch?v=VIDEO_ID" --positions 0.25 0.5 0.75
youtube-cli inspect --results youtube-cli-output\search-YYYYMMDD-HHMMSS\results.json --index 3
youtube-cli download "https://www.youtube.com/watch?v=VIDEO_ID" --out downloads
youtube-cli transcript "https://www.youtube.com/watch?v=VIDEO_ID" --langs en
youtube-cli session "best free video editors" --limit 5
```

## Output Folders

- `youtube-cli-output/search-*` for search screenshots and `results.json`
- `youtube-cli-output/view-*` for frame screenshots and `frames.json`
- `youtube-cli-output/inspect-*` for URL or result-index inspections
- `youtube-cli-output/session-*` for guided agent sessions

## Flow

1. Search a topic.
2. Inspect promising results.
3. Capture frames or transcripts.
4. Download the assets you actually need.

## Notes

- `--profile-dir` is useful when YouTube login or cookies are needed.
- `--browser-binary` lets you point at a local Chromium install.
- `--headless-new` is available when you want Chromium's newer headless mode.

