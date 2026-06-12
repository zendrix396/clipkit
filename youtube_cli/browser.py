from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterable
from urllib.parse import quote_plus, urlparse, parse_qs


YOUTUBE_HOME = "https://www.youtube.com"
DEFAULT_POSITIONS = ("0.25", "0.5", "0.75")


@dataclass(frozen=True)
class BrowserSettings:
    headless: bool = False
    headless_new: bool = False
    browser_binary: str | None = None
    profile_dir: Path | None = None
    timeout: int = 60
    wait: float = 2.0
    quality: int = 90


@dataclass(frozen=True)
class SearchResult:
    index: int
    title: str
    url: str
    video_id: str
    channel: str = ""
    metadata: str = ""
    thumbnail: str = ""


@dataclass(frozen=True)
class SearchOutput:
    query: str
    url: str
    screenshot: str
    results_json: str
    results: list[SearchResult]


@dataclass(frozen=True)
class FrameCapture:
    label: str
    seconds: float
    current_time: float | None
    screenshot: str
    clicked_progress_bar: bool
    visible_caption: str = ""
    dialogue: list["DialogueLine"] | None = None


@dataclass(frozen=True)
class DialogueLine:
    text: str
    start: float
    duration: float


@dataclass(frozen=True)
class FrameOutput:
    url: str
    title: str
    duration: float | None
    page_screenshot: str
    frames_json: str
    frames: list[FrameCapture]


def timestamped_dir(parent: Path, prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return parent / f"{prefix}-{stamp}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def search_output_to_json(output: SearchOutput) -> dict[str, Any]:
    return {
        "query": output.query,
        "url": output.url,
        "screenshot": output.screenshot,
        "results": [asdict(result) for result in output.results],
    }


def frames_output_to_json(output: FrameOutput) -> dict[str, Any]:
    return {
        "url": output.url,
        "title": output.title,
        "duration": output.duration,
        "page_screenshot": output.page_screenshot,
        "frames": [asdict(frame) for frame in output.frames],
    }


def video_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/") or None
    if "youtube.com" in host:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        if parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
            parts = [part for part in parsed.path.split("/") if part]
            return parts[1] if len(parts) > 1 else None
    return None


def normalize_youtube_url(url: str) -> str:
    video_id = video_id_from_url(url)
    if not video_id:
        return url
    return f"{YOUTUBE_HOME}/watch?v={video_id}"


def parse_position(value: str, duration: float | None) -> tuple[str, float]:
    raw = value.strip()
    if not raw:
        raise ValueError("position cannot be empty")

    if raw.endswith("%"):
        if duration is None:
            raise ValueError(f"duration is unknown, cannot resolve percentage {raw}")
        pct = float(raw[:-1]) / 100.0
        return raw, max(0.0, duration * pct)

    if ":" in raw:
        parts = [float(part) for part in raw.split(":")]
        if len(parts) == 2:
            seconds = parts[0] * 60 + parts[1]
        elif len(parts) == 3:
            seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
            raise ValueError(f"invalid timestamp position: {raw}")
        return raw, max(0.0, seconds)

    if raw.lower().endswith("s"):
        return raw, max(0.0, float(raw[:-1]))

    number = float(raw)
    if 0.0 <= number <= 1.0 and duration is not None:
        return raw, max(0.0, duration * number)
    return raw, max(0.0, number)


def _response_value(response: Any) -> Any:
    if response is None:
        return None

    if isinstance(response, dict):
        if "value" in response and ("type" in response or len(response) == 1):
            return response["value"]

        result = response.get("result")
        if isinstance(result, dict):
            nested_result = result.get("result")
            if isinstance(nested_result, dict):
                return _response_value(nested_result)
            if "value" in result:
                return result["value"]
            if "description" in result:
                return result["description"]
        if "value" in response:
            return response["value"]
        return response

    result = getattr(response, "result", None)
    if result is not None:
        if isinstance(result, dict):
            nested_result = result.get("result")
            if isinstance(nested_result, dict):
                return _response_value(nested_result)
            if "value" in result:
                return result["value"]
            if "description" in result:
                return result["description"]
        value = getattr(result, "value", None)
        if value is not None:
            return value
        description = getattr(result, "description", None)
        if description is not None:
            return description

    value = getattr(response, "value", None)
    if value is not None:
        return value

    return response


@asynccontextmanager
async def open_browser(settings: BrowserSettings) -> AsyncIterator[tuple[Any, Any]]:
    try:
        from pydoll.browser.chromium import Chrome
        from pydoll.browser.options import ChromiumOptions
    except ImportError as exc:
        raise RuntimeError(
            "Missing pydoll-python. Install dependencies with: python -m pip install -e ."
        ) from exc

    options = ChromiumOptions()
    options.start_timeout = settings.timeout
    if settings.headless_new:
        options.add_argument("--headless=new")
    else:
        options.headless = settings.headless
    options.block_notifications = True
    options.password_manager_enabled = False
    options.add_argument("--lang=en-US")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-breakpad")
    options.add_argument("--disable-crash-reporter")
    options.add_argument("--autoplay-policy=no-user-gesture-required")

    if settings.browser_binary:
        options.binary_location = settings.browser_binary

    if settings.profile_dir:
        profile_dir = settings.profile_dir.resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        preferences_path = profile_dir / "Default" / "Preferences"
        preferences_path.parent.mkdir(parents=True, exist_ok=True)
        if not preferences_path.exists():
            preferences_path.write_text("{}", encoding="utf-8")
        options.add_argument(f"--user-data-dir={profile_dir}")

    async with Chrome(options=options) as browser:
        tab = await browser.start()
        yield browser, tab


async def dismiss_youtube_popups(tab: Any) -> None:
    script = r"""
(() => {
  const needles = [
    "accept all",
    "i agree",
    "reject all",
    "no thanks",
    "skip trial",
    "got it",
    "not now"
  ];
  const buttons = Array.from(document.querySelectorAll("button, tp-yt-paper-button, ytd-button-renderer"));
  for (const button of buttons) {
    const text = (button.innerText || button.textContent || "").trim().toLowerCase();
    if (needles.some((needle) => text.includes(needle))) {
      button.click();
      return text;
    }
  }
  return "";
})()
"""
    try:
        await tab.execute_script(script, return_by_value=True)
        await asyncio.sleep(0.5)
    except Exception:
        return


async def search_youtube(
    query: str,
    output_dir: Path,
    settings: BrowserSettings,
    *,
    limit: int = 8,
    scrolls: int = 2,
    full_page: bool = False,
) -> SearchOutput:
    output_dir.mkdir(parents=True, exist_ok=True)
    url = f"{YOUTUBE_HOME}/results?search_query={quote_plus(query)}"

    async with open_browser(settings) as (_, tab):
        await tab.go_to(url, timeout=settings.timeout)
        await asyncio.sleep(settings.wait)
        await dismiss_youtube_popups(tab)

        for _ in range(max(0, scrolls)):
            await tab.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * 0.85))")
            await asyncio.sleep(0.8)

        raw_results = await _extract_search_results(tab, limit)
        screenshot_path = output_dir / "search.png"
        await tab.take_screenshot(screenshot_path, quality=settings.quality, beyond_viewport=full_page)

    results = [
        SearchResult(
            index=index + 1,
            title=str(item.get("title") or "").strip(),
            url=str(item.get("url") or "").strip(),
            video_id=str(item.get("video_id") or "").strip(),
            channel=str(item.get("channel") or "").strip(),
            metadata=str(item.get("metadata") or "").strip(),
            thumbnail=str(item.get("thumbnail") or "").strip(),
        )
        for index, item in enumerate(raw_results)
        if item.get("url") and item.get("video_id")
    ]

    results_json = output_dir / "results.json"
    output = SearchOutput(
        query=query,
        url=url,
        screenshot=str(screenshot_path),
        results_json=str(results_json),
        results=results,
    )
    write_json(results_json, search_output_to_json(output))
    return output


async def _extract_search_results(tab: Any, limit: int) -> list[dict[str, Any]]:
    script = f"""
(() => {{
  const limit = {int(limit)};
  const anchors = Array.from(document.querySelectorAll(
    'ytd-video-renderer a#video-title[href*="/watch"], ytd-compact-video-renderer a#video-title[href*="/watch"], ytd-rich-item-renderer a#video-title-link[href*="/watch"], a#video-title[href*="/watch"]'
  ));
  const seen = new Set();
  const items = [];

  for (const anchor of anchors) {{
    const href = anchor.href || anchor.getAttribute("href") || "";
    if (!href.includes("/watch")) continue;

    let parsed;
    try {{
      parsed = new URL(href, location.origin);
    }} catch (_) {{
      continue;
    }}

    const videoId = parsed.searchParams.get("v");
    if (!videoId || seen.has(videoId)) continue;
    seen.add(videoId);

    const renderer = anchor.closest("ytd-video-renderer,ytd-rich-item-renderer,ytd-compact-video-renderer") || anchor;
    const titleEl = renderer.querySelector("#video-title") || renderer.querySelector("a[title]") || anchor;
    const channelEl = renderer.querySelector("ytd-channel-name a, #channel-name a, .ytd-channel-name a");
    const meta = Array.from(renderer.querySelectorAll("#metadata-line span, .inline-metadata-item"))
      .map((node) => (node.textContent || "").trim())
      .filter(Boolean);
    const image = renderer.querySelector("img");

    const title = (titleEl.textContent || titleEl.getAttribute("title") || "").trim();
    if (!title) continue;

    items.push({{
      title,
      url: `${{location.origin}}/watch?v=${{videoId}}`,
      video_id: videoId,
      channel: channelEl ? (channelEl.textContent || "").trim() : "",
      metadata: meta.join(" | "),
      thumbnail: image ? (image.currentSrc || image.src || "") : ""
    }});

    if (items.length >= limit) break;
  }}

  return items;
}})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    value = _response_value(response)
    return value if isinstance(value, list) else []


async def capture_video_frames(
    url: str,
    output_dir: Path,
    settings: BrowserSettings,
    *,
    positions: Iterable[str] = DEFAULT_POSITIONS,
    video_only: bool = False,
    humanize: bool = True,
    captions: bool = True,
    caption_languages: Iterable[str] = ("en",),
    dialogue_window: float = 8.0,
) -> FrameOutput:
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_url = normalize_youtube_url(url)
    transcript = _load_dialogue(normalized_url, list(caption_languages)) if captions else []

    async with open_browser(settings) as (_, tab):
        await tab.go_to(normalized_url, timeout=settings.timeout)
        await asyncio.sleep(settings.wait)
        await dismiss_youtube_popups(tab)

        video_info = await _wait_for_video(tab, timeout_ms=settings.timeout * 1000)
        title = str(video_info.get("title") or "")
        duration = _coerce_float(video_info.get("duration"))
        if captions:
            await _enable_youtube_captions(tab)

        page_screenshot = output_dir / "video-page.png"
        await tab.take_screenshot(page_screenshot, quality=settings.quality)

        frames: list[FrameCapture] = []
        for position in positions:
            label, seconds = parse_position(position, duration)
            if duration:
                seconds = min(seconds, max(0.0, duration - 0.25))
            clicked = await _click_progress_bar(tab, seconds, duration, humanize=humanize)
            current_time = await _ensure_video_time(tab, seconds)
            if captions:
                await _brief_play_for_captions(tab)
            await asyncio.sleep(0.7)
            visible_caption = await _read_visible_captions(tab) if captions else ""
            dialogue = _dialogue_near(transcript, current_time or seconds, dialogue_window)

            safe_label = _safe_label(label)
            screenshot = output_dir / f"frame-{safe_label}.png"
            captured = False
            if video_only:
                captured = await _take_player_screenshot(tab, screenshot, settings.quality)
            if not captured:
                await tab.take_screenshot(screenshot, quality=settings.quality)

            frames.append(
                FrameCapture(
                    label=label,
                    seconds=seconds,
                    current_time=current_time,
                    screenshot=str(screenshot),
                    clicked_progress_bar=clicked,
                    visible_caption=visible_caption,
                    dialogue=dialogue,
                )
            )

    frames_json = output_dir / "frames.json"
    output = FrameOutput(
        url=normalized_url,
        title=title,
        duration=duration,
        page_screenshot=str(page_screenshot),
        frames_json=str(frames_json),
        frames=frames,
    )
    write_json(frames_json, frames_output_to_json(output))
    return output


async def _wait_for_video(tab: Any, timeout_ms: int) -> dict[str, Any]:
    script = f"""
new Promise((resolve) => {{
  const started = Date.now();
  const done = (video) => resolve({{
    title: document.title || "",
    duration: Number.isFinite(video.duration) ? video.duration : null,
    currentTime: Number.isFinite(video.currentTime) ? video.currentTime : null
  }});
  const tick = () => {{
    const video = document.querySelector("video");
    if (video && Number.isFinite(video.duration) && video.duration > 0) {{
      video.pause();
      done(video);
      return;
    }}
    if (Date.now() - started > {int(timeout_ms)}) {{
      resolve({{ title: document.title || "", duration: null, currentTime: null }});
      return;
    }}
    setTimeout(tick, 250);
  }};
  tick();
}})
"""
    response = await tab.execute_script(script, return_by_value=True, await_promise=True)
    value = _response_value(response)
    return value if isinstance(value, dict) else {}


async def _enable_youtube_captions(tab: Any) -> bool:
    script = r"""
(() => {
  const button = document.querySelector(".ytp-subtitles-button");
  if (!button) return false;
  const pressed = button.getAttribute("aria-pressed") === "true";
  if (!pressed) button.click();
  return true;
})()
"""
    try:
        response = await tab.execute_script(script, return_by_value=True)
        await asyncio.sleep(0.5)
        return bool(_response_value(response))
    except Exception:
        return False


async def _brief_play_for_captions(tab: Any) -> None:
    script = r"""
new Promise((resolve) => {
  const video = document.querySelector("video");
  if (!video) {
    resolve(false);
    return;
  }

  video.muted = true;
  const done = () => {
    video.pause();
    resolve(true);
  };

  const play = video.play();
  if (play && typeof play.catch === "function") {
    play.catch(() => {});
  }
  setTimeout(done, 900);
})
"""
    try:
        await tab.execute_script(script, return_by_value=True, await_promise=True)
    except Exception:
        return


async def _read_visible_captions(tab: Any) -> str:
    script = r"""
(() => Array.from(document.querySelectorAll(".ytp-caption-segment"))
  .map((node) => (node.textContent || "").trim())
  .filter(Boolean)
  .join(" "))()
"""
    try:
        response = await tab.execute_script(script, return_by_value=True)
    except Exception:
        return ""
    value = _response_value(response)
    return str(value or "").strip()


async def _click_progress_bar(
    tab: Any,
    seconds: float,
    duration: float | None,
    *,
    humanize: bool,
) -> bool:
    if not duration or duration <= 0:
        return False

    rect = await _progress_bar_rect(tab)
    if not rect:
        return False

    width = _coerce_float(rect.get("width")) or 0.0
    height = _coerce_float(rect.get("height")) or 0.0
    left = _coerce_float(rect.get("left")) or 0.0
    top = _coerce_float(rect.get("top")) or 0.0
    if width <= 0 or height <= 0:
        return False

    fraction = max(0.0, min(1.0, seconds / duration))
    x = left + (width * fraction)
    y = top + (height / 2.0)

    try:
        await tab.mouse.click(x, y, humanize=humanize)
        await asyncio.sleep(0.8)
        return True
    except Exception:
        return False


async def _progress_bar_rect(tab: Any) -> dict[str, Any] | None:
    script = r"""
(() => {
  const player = document.querySelector("#movie_player, .html5-video-player");
  if (player) {
    const rect = player.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.bottom - 44;
    const event = new MouseEvent("mousemove", { bubbles: true, clientX: x, clientY: y });
    player.dispatchEvent(event);
  }

  const bar = document.querySelector(".ytp-progress-bar")
    || document.querySelector(".ytp-progress-list")
    || document.querySelector('[role="slider"][aria-label*="Seek"]');
  if (!bar) return null;

  const rect = bar.getBoundingClientRect();
  return {
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height
  };
})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    value = _response_value(response)
    return value if isinstance(value, dict) else None


async def _ensure_video_time(tab: Any, seconds: float) -> float | None:
    script = f"""
new Promise((resolve) => {{
  const video = document.querySelector("video");
  if (!video) {{
    resolve(null);
    return;
  }}

  const target = {seconds:.6f};
  const finish = () => {{
    video.pause();
    resolve(Number.isFinite(video.currentTime) ? video.currentTime : null);
  }};

  if (Math.abs(video.currentTime - target) <= 1.25) {{
    finish();
    return;
  }}

  const timer = setTimeout(finish, 1800);
  video.addEventListener("seeked", () => {{
    clearTimeout(timer);
    finish();
  }}, {{ once: true }});
  video.currentTime = target;
}})
"""
    response = await tab.execute_script(script, return_by_value=True, await_promise=True)
    return _coerce_float(_response_value(response))


async def _take_player_screenshot(tab: Any, path: Path, quality: int) -> bool:
    for selector in ("#movie_player", ".html5-video-player", "video"):
        captured = await _take_element_screenshot(tab, selector, path, quality)
        if captured:
            return True
    return False


async def _take_element_screenshot(tab: Any, selector: str, path: Path, quality: int) -> bool:
    try:
        element = await tab.query(selector, timeout=5, raise_exc=False)
        if not element:
            return False
        await element.take_screenshot(path, quality=quality)
        return True
    except Exception:
        return False


def _load_dialogue(url: str, languages: list[str]) -> list[DialogueLine]:
    try:
        from .downloader import load_transcript_segments

        return [
            DialogueLine(text=item.text, start=item.start, duration=item.duration)
            for item in load_transcript_segments(url, languages=languages)
        ]
    except Exception:
        return []


def _dialogue_near(
    transcript: list[DialogueLine],
    seconds: float,
    window: float,
) -> list[DialogueLine]:
    start = max(0.0, seconds - window)
    end = seconds + window
    return [
        line
        for line in transcript
        if line.start <= end and (line.start + line.duration) >= start
    ]


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_label(value: str) -> str:
    label = value.strip().lower().replace("%", "pct").replace(":", "-")
    label = re.sub(r"[^a-z0-9._-]+", "-", label)
    return label.strip("-") or "frame"
