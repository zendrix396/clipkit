from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start: float
    duration: float


@dataclass(frozen=True)
class TranscriptOutput:
    video_id: str
    text_path: str
    json_path: str
    segments: list[TranscriptSegment]


def video_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/")
    elif "youtube.com" in host and parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    elif "youtube.com" in host and (parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/")):
        parts = [part for part in parsed.path.split("/") if part]
        video_id = parts[1] if len(parts) > 1 else ""
    else:
        video_id = ""

    if not video_id:
        raise ValueError(f"Could not parse YouTube video id from: {url}")
    return video_id


def safe_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "", value).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:160] or "youtube"


def split_languages(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = []
        for item in value:
            parts.extend(str(item).split(","))
    return [part.strip() for part in parts if part.strip()]


def download_media(
    url: str,
    output_dir: Path,
    *,
    audio_only: bool = False,
    mode: str | None = None,
    audio_format: str = "wav",
    subtitles: bool = False,
    auto_subs: bool = False,
    languages: list[str] | None = None,
    remote_components: bool = True,
) -> dict[str, Any]:
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise RuntimeError("Missing yt-dlp. Install dependencies with: python -m pip install -e .") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    languages = languages or ["en"]
    selected_mode = mode or ("audio" if audio_only else "video")
    if selected_mode not in {"video", "audio", "both"}:
        raise ValueError("mode must be one of: video, audio, both")

    if selected_mode == "both":
        video = download_media(
            url,
            output_dir,
            mode="video",
            subtitles=subtitles,
            auto_subs=auto_subs,
            languages=languages,
            remote_components=remote_components,
        )
        audio = download_media(
            url,
            output_dir,
            mode="audio",
            audio_format=audio_format,
            languages=languages,
            remote_components=remote_components,
        )
        return {
            "mode": "both",
            "video": video,
            "audio": audio,
            "output_dir": str(output_dir),
        }

    before = _snapshot_files(output_dir)
    suffix = ".audio" if selected_mode == "audio" else ""
    outtmpl = str(output_dir / f"%(title).200B [%(id)s]{suffix}.%(ext)s")

    options: dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "format": "bestaudio/best" if selected_mode == "audio" else "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "quiet": False,
        "ignoreerrors": False,
        "retries": 10,
        "fragment_retries": 10,
        "continuedl": True,
    }
    js_runtime = _available_js_runtime()
    if js_runtime:
        name, executable = js_runtime
        options["js_runtimes"] = {name: {"path": executable}}
    if remote_components:
        options["remote_components"] = ["ejs:github"]

    if selected_mode == "audio":
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "0",
            }
        ]
    else:
        options["merge_output_format"] = "mp4"

    if subtitles:
        options["writesubtitles"] = True
        options["subtitleslangs"] = languages
        options["subtitlesformat"] = "vtt/best"

    if auto_subs:
        options["writeautomaticsub"] = True
        options["subtitleslangs"] = languages
        options["subtitlesformat"] = "vtt/best"

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    after = _snapshot_files(output_dir)
    video_id = info.get("id") if isinstance(info, dict) else None
    files = after - before
    if not files and video_id:
        files = {path for path in after if f"[{video_id}]" in path.name}
    return {
        "mode": selected_mode,
        "id": video_id,
        "title": info.get("title") if isinstance(info, dict) else None,
        "output_dir": str(output_dir),
        "files": sorted(str(path) for path in files),
    }


def fetch_transcript(
    url: str,
    output_dir: Path,
    *,
    languages: list[str] | None = None,
) -> TranscriptOutput:
    output_dir.mkdir(parents=True, exist_ok=True)
    video_id = video_id_from_url(url)
    languages = languages or ["en"]

    segments = load_transcript_segments(url, languages=languages)
    text = "\n".join(segment.text for segment in segments)

    base = output_dir / safe_filename(video_id)
    text_path = base.with_suffix(".txt")
    json_path = base.with_suffix(".json")

    text_path.write_text(text, encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "video_id": video_id,
                "languages": languages,
                "segments": [asdict(segment) for segment in segments],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return TranscriptOutput(
        video_id=video_id,
        text_path=str(text_path),
        json_path=str(json_path),
        segments=segments,
    )


def load_transcript_segments(
    url: str,
    *,
    languages: list[str] | None = None,
) -> list[TranscriptSegment]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise RuntimeError(
            "Missing youtube-transcript-api. Install dependencies with: python -m pip install -e ."
        ) from exc

    video_id = video_id_from_url(url)
    languages = languages or ["en"]

    api = YouTubeTranscriptApi()
    if hasattr(api, "fetch"):
        raw = api.fetch(video_id, languages=languages)
    else:
        raw = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)

    return [_segment_from_raw(item) for item in raw]


def _segment_from_raw(item: Any) -> TranscriptSegment:
    if isinstance(item, dict):
        return TranscriptSegment(
            text=_clean_transcript_text(str(item.get("text") or "")),
            start=float(item.get("start") or 0.0),
            duration=float(item.get("duration") or 0.0),
        )

    return TranscriptSegment(
        text=_clean_transcript_text(str(getattr(item, "text", ""))),
        start=float(getattr(item, "start", 0.0) or 0.0),
        duration=float(getattr(item, "duration", 0.0) or 0.0),
    )


def _clean_transcript_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _snapshot_files(path: Path) -> set[Path]:
    if not path.exists():
        return set()
    return {item.resolve() for item in path.rglob("*") if item.is_file()}


def _available_js_runtime() -> tuple[str, str] | None:
    for name in ("node", "bun", "deno"):
        executable = shutil.which(name)
        if executable:
            return name, executable
    return None
