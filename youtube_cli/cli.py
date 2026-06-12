from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .browser import (
    DEFAULT_POSITIONS,
    BrowserSettings,
    capture_video_frames,
    search_output_to_json,
    search_youtube,
    timestamped_dir,
)
from .downloader import download_media, fetch_transcript, split_languages


DEFAULT_OUTPUT = Path("youtube-cli-output")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "search":
            output = asyncio.run(run_search(args))
            print_search_output(output)
            return 0
        if args.command == "view":
            output = asyncio.run(run_view(args))
            print_frame_output(output)
            return 0
        if args.command == "inspect":
            output = asyncio.run(run_inspect(args))
            print_frame_output(output)
            return 0
        if args.command == "download":
            mode = args.mode
            if args.audio_only:
                mode = "audio"
            if args.video_only:
                mode = "video"
            info = download_media(
                args.url,
                Path(args.out),
                mode=mode,
                audio_format=args.audio_format,
                subtitles=args.subtitles,
                auto_subs=args.auto_subs,
                languages=split_languages(args.langs),
                remote_components=args.remote_components,
            )
            print(json.dumps(info, indent=2, ensure_ascii=False))
            return 0
        if args.command == "transcript":
            output = fetch_transcript(args.url, Path(args.out), languages=split_languages(args.langs))
            print(json.dumps(asdict(output), indent=2, ensure_ascii=False))
            return 0
        if args.command == "session":
            return asyncio.run(run_session(args))
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtube-cli",
        description="Browse YouTube with Pydoll, save screenshots, inspect frames, download media, and fetch transcripts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Search YouTube and save a result-page screenshot.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--scrolls", type=int, default=2)
    search.add_argument("--full-page", action="store_true")
    add_browser_args(search)
    search.add_argument("--out", default=None)

    view = subparsers.add_parser("view", help="Open a video, click through positions, and save frame screenshots.")
    view.add_argument("url")
    view.add_argument("--positions", nargs="+", default=list(DEFAULT_POSITIONS))
    view.add_argument("--video-only", action="store_true", help="Capture the video element instead of the whole page.")
    view.add_argument("--no-humanize", action="store_true", help="Disable humanized progress bar clicks.")
    add_caption_args(view)
    add_browser_args(view)
    view.add_argument("--out", default=None)

    inspect_cmd = subparsers.add_parser(
        "inspect",
        help="Inspect a URL or a saved search result index with screenshots and dialogue context.",
    )
    inspect_cmd.add_argument("url", nargs="?")
    inspect_cmd.add_argument("--results", help="Path to a search results.json file.")
    inspect_cmd.add_argument("--index", type=int, help="1-based result index from results.json.")
    inspect_cmd.add_argument("--positions", nargs="+", default=list(DEFAULT_POSITIONS))
    inspect_cmd.add_argument("--video-only", action="store_true")
    inspect_cmd.add_argument("--no-humanize", action="store_true")
    add_caption_args(inspect_cmd)
    add_browser_args(inspect_cmd)
    inspect_cmd.add_argument("--out", default=None)

    download = subparsers.add_parser("download", help="Download MP4 video, WAV audio, or both with yt-dlp.")
    download.add_argument("url")
    download.add_argument("--out", default="downloads")
    download.add_argument("--mode", choices=["video", "audio", "both"], default="both")
    download.add_argument("--audio-only", action="store_true")
    download.add_argument("--video-only", action="store_true")
    download.add_argument("--audio-format", default="wav")
    download.add_argument("--subtitles", action=argparse.BooleanOptionalAction, default=True)
    download.add_argument("--auto-subs", action=argparse.BooleanOptionalAction, default=True)
    download.add_argument("--remote-components", action=argparse.BooleanOptionalAction, default=True)
    download.add_argument("--langs", default="en")

    transcript = subparsers.add_parser("transcript", help="Save transcript text and JSON.")
    transcript.add_argument("url")
    transcript.add_argument("--out", default="transcripts")
    transcript.add_argument("--langs", default="en")

    session = subparsers.add_parser("session", help="Guided search, inspect, download, and transcript workflow.")
    session.add_argument("query")
    session.add_argument("--limit", type=int, default=5)
    session.add_argument("--positions", nargs="+", default=list(DEFAULT_POSITIONS))
    session.add_argument("--video-only", action="store_true")
    session.add_argument("--download-out", default="downloads")
    session.add_argument("--transcript-out", default="transcripts")
    session.add_argument("--audio-format", default="wav")
    add_caption_args(session)
    add_browser_args(session)
    session.add_argument("--out", default=None)

    return parser


def add_browser_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headless-new", action="store_true", help="Use Chromium's --headless=new mode.")
    parser.add_argument("--browser-binary", default=None)
    parser.add_argument("--profile-dir", default=None)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--wait", type=float, default=2.0)
    parser.add_argument("--quality", type=int, default=90)


def add_caption_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--captions", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--langs", default="en")
    parser.add_argument("--dialogue-window", type=float, default=8.0)


def settings_from_args(args: argparse.Namespace) -> BrowserSettings:
    return BrowserSettings(
        headless=bool(args.headless),
        headless_new=bool(args.headless_new),
        browser_binary=args.browser_binary,
        profile_dir=Path(args.profile_dir) if args.profile_dir else None,
        timeout=int(args.timeout),
        wait=float(args.wait),
        quality=int(args.quality),
    )


async def run_search(args: argparse.Namespace):
    output_dir = Path(args.out) if args.out else timestamped_dir(DEFAULT_OUTPUT, "search")
    return await search_youtube(
        args.query,
        output_dir,
        settings_from_args(args),
        limit=args.limit,
        scrolls=args.scrolls,
        full_page=args.full_page,
    )


async def run_view(args: argparse.Namespace):
    output_dir = Path(args.out) if args.out else timestamped_dir(DEFAULT_OUTPUT, "view")
    return await capture_video_frames(
        args.url,
        output_dir,
        settings_from_args(args),
        positions=args.positions,
        video_only=args.video_only,
        humanize=not args.no_humanize,
        captions=args.captions,
        caption_languages=split_languages(args.langs),
        dialogue_window=args.dialogue_window,
    )


async def run_inspect(args: argparse.Namespace):
    url = resolve_inspect_url(args)
    output_dir = Path(args.out) if args.out else timestamped_dir(DEFAULT_OUTPUT, "inspect")
    return await capture_video_frames(
        url,
        output_dir,
        settings_from_args(args),
        positions=args.positions,
        video_only=args.video_only,
        humanize=not args.no_humanize,
        captions=args.captions,
        caption_languages=split_languages(args.langs),
        dialogue_window=args.dialogue_window,
    )


async def run_session(args: argparse.Namespace) -> int:
    root = Path(args.out) if args.out else timestamped_dir(DEFAULT_OUTPUT, "session")
    search_dir = root / "search"
    view_dir = root / "view"

    search_output = await search_youtube(
        args.query,
        search_dir,
        settings_from_args(args),
        limit=args.limit,
        scrolls=2,
        full_page=False,
    )
    print_search_output(search_output)

    if not search_output.results:
        print("No video results found.")
        return 1

    selected = prompt_for_result(search_output.results)
    if selected is None:
        return 0

    frame_output = await capture_video_frames(
        selected.url,
        view_dir,
        settings_from_args(args),
        positions=args.positions,
        video_only=args.video_only,
        humanize=True,
        captions=args.captions,
        caption_languages=split_languages(args.langs),
        dialogue_window=args.dialogue_window,
    )
    print_frame_output(frame_output)

    while True:
        action = input("Action: [d]ownload, [t]ranscript, [b]oth, [q]uit: ").strip().lower()
        if action in {"q", "quit", ""}:
            return 0
        if action in {"d", "download", "b", "both"}:
            info = download_media(
                selected.url,
                Path(args.download_out),
                mode="both",
                audio_format=args.audio_format,
                subtitles=True,
                auto_subs=True,
                languages=split_languages(args.langs),
                remote_components=True,
            )
            print(json.dumps(info, indent=2, ensure_ascii=False))
        if action in {"t", "transcript", "b", "both"}:
            transcript = fetch_transcript(
                selected.url,
                Path(args.transcript_out),
                languages=split_languages(args.langs),
            )
            print(json.dumps(asdict(transcript), indent=2, ensure_ascii=False))
        if action in {"d", "download", "t", "transcript", "b", "both"}:
            return 0
        print("Unknown action.")


def resolve_inspect_url(args: argparse.Namespace) -> str:
    if args.results:
        if args.index is None:
            raise ValueError("--index is required when --results is used")
        return result_url_from_json(Path(args.results), args.index)
    if args.url:
        return args.url
    raise ValueError("provide a URL or use --results with --index")


def result_url_from_json(path: Path, index: int) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise ValueError(f"No results list found in {path}")

    for item in results:
        if isinstance(item, dict) and int(item.get("index", -1)) == index:
            url = str(item.get("url") or "")
            if url:
                return url

    fallback_index = index - 1
    if 0 <= fallback_index < len(results):
        item = results[fallback_index]
        if isinstance(item, dict) and item.get("url"):
            return str(item["url"])

    raise ValueError(f"Result index {index} not found in {path}")


def prompt_for_result(results):
    while True:
        value = input("Select video number to inspect, or q to quit: ").strip().lower()
        if value in {"q", "quit", ""}:
            return None
        try:
            index = int(value)
        except ValueError:
            print("Enter a result number.")
            continue
        for result in results:
            if result.index == index:
                return result
        print("That result number is not in the list.")


def print_search_output(output) -> None:
    print(f"Screenshot: {output.screenshot}")
    print(f"Results JSON: {output.results_json}")
    print()
    for result in output.results:
        metadata = f" | {result.metadata}" if result.metadata else ""
        channel = f" - {result.channel}" if result.channel else ""
        print(f"{result.index}. {result.title}{channel}{metadata}")
        print(f"   {result.url}")

    if not output.results:
        print(json.dumps(search_output_to_json(output), indent=2, ensure_ascii=False))
    else:
        print()
        print(f"Inspect a result: youtube-cli inspect --results {output.results_json} --index <number>")


def print_frame_output(output) -> None:
    print(f"Page screenshot: {output.page_screenshot}")
    print(f"Frames JSON: {output.frames_json}")
    if output.title:
        print(f"Title: {output.title}")
    if output.duration:
        print(f"Duration: {output.duration:.2f}s")
    print()
    for frame in output.frames:
        current = "" if frame.current_time is None else f" current={frame.current_time:.2f}s"
        clicked = "clicked" if frame.clicked_progress_bar else "js-seek"
        print(f"{frame.label}: {frame.screenshot} ({clicked},{current})")
        if frame.visible_caption:
            print(f"   visible caption: {frame.visible_caption}")
        for line in (frame.dialogue or [])[:5]:
            print(f"   [{line.start:.2f}s] {line.text}")
