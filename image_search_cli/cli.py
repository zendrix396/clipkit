from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.error import URLError
from urllib.parse import parse_qs, quote_plus, unquote_to_bytes, urlparse
from urllib.request import Request, urlopen

from youtube_cli.browser import BrowserSettings, _response_value, open_browser, timestamped_dir, write_json


DEFAULT_OUTPUT = Path("image-search-output")
GOOGLE_IMAGES_URL = "https://www.google.com/search?tbm=isch&hl=en&q={query}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class ImageResult:
    index: int
    title: str
    source_url: str
    thumbnail_url: str
    page_url: str
    width: int
    height: int
    rendered_width: int
    rendered_height: int
    path: str
    method: str
    error: str = ""


@dataclass(frozen=True)
class ImageSearchOutput:
    query: str
    url: str
    screenshot: str
    results_json: str
    results: list[ImageResult]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        output_dir = Path(args.out) if args.out else timestamped_dir(DEFAULT_OUTPUT, "search")
        output = asyncio.run(
            search_google_images(
                args.query,
                output_dir,
                settings_from_args(args),
                limit=args.limit,
                scrolls=args.scrolls,
                full_page=args.full_page,
                download_timeout=args.download_timeout,
            )
        )
        print(json.dumps(image_output_to_json(output), indent=2, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="image-search-cli",
        description="Search Google Images with Pydoll and save the top image results.",
    )
    parser.add_argument("query", help="Google Images search query.")
    parser.add_argument("--limit", type=int, default=1, help="Number of top images to save.")
    parser.add_argument("--scrolls", type=int, default=1, help="Extra result-page scrolls before extraction.")
    parser.add_argument("--full-page", action="store_true", help="Capture the full Google Images page.")
    parser.add_argument("--out", default=None, help="Output directory. Defaults to image-search-output/search-*.")
    parser.add_argument("--download-timeout", type=int, default=20)
    add_browser_args(parser)
    return parser


def add_browser_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headless-new", action="store_true", help="Use Chromium's --headless=new mode.")
    parser.add_argument("--browser-binary", default=None)
    parser.add_argument("--profile-dir", default=None)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--wait", type=float, default=2.0)
    parser.add_argument("--quality", type=int, default=90)


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


async def search_google_images(
    query: str,
    output_dir: Path,
    settings: BrowserSettings,
    *,
    limit: int = 1,
    scrolls: int = 1,
    full_page: bool = False,
    download_timeout: int = 20,
) -> ImageSearchOutput:
    if limit <= 0:
        raise ValueError("--limit must be greater than zero")

    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    url = GOOGLE_IMAGES_URL.format(query=quote_plus(query))

    async with open_browser(settings) as (_, tab):
        await tab.go_to(url, timeout=settings.timeout)
        await asyncio.sleep(settings.wait)
        await dismiss_google_popups(tab)
        await wait_for_google_images(tab, min(limit, 3), settings.timeout)

        raw_results = await extract_image_results(tab, max(limit * 3, limit + 6))
        for _ in range(max(0, scrolls)):
            if len(raw_results) >= limit:
                break
            await tab.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * 0.85))")
            await asyncio.sleep(0.8)
            raw_results = await extract_image_results(tab, max(limit * 3, limit + 6))

        screenshot_path = output_dir / "image-tab.png"
        await tab.take_screenshot(screenshot_path, quality=settings.quality, beyond_viewport=full_page)

        if not raw_results:
            if await google_blocked(tab):
                raise RuntimeError(
                    f"Google returned a CAPTCHA/unusual-traffic page. Screenshot saved at {screenshot_path}. "
                    "Retry visible mode with --profile-dir."
                )
            raise RuntimeError(f"No Google Images results found. Screenshot saved at {screenshot_path}.")

        results: list[ImageResult] = []
        for raw in raw_results[:limit]:
            index = len(results) + 1
            result = await save_image_result(
                tab,
                raw,
                images_dir,
                index,
                referer=url,
                quality=settings.quality,
                timeout=download_timeout,
            )
            results.append(result)

    results_json = output_dir / "results.json"
    output = ImageSearchOutput(
        query=query,
        url=url,
        screenshot=str(screenshot_path),
        results_json=str(results_json),
        results=results,
    )
    write_json(results_json, image_output_to_json(output))
    return output


async def dismiss_google_popups(tab: Any) -> None:
    script = r"""
(() => {
  const needles = [
    "accept all",
    "i agree",
    "reject all",
    "no thanks",
    "not now",
    "got it",
    "stay signed out"
  ];
  const nodes = Array.from(document.querySelectorAll("button, div[role='button'], a[role='button']"));
  for (const node of nodes) {
    const text = (node.innerText || node.textContent || "").trim().toLowerCase();
    if (needles.some((needle) => text.includes(needle))) {
      node.click();
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


async def wait_for_google_images(tab: Any, minimum: int, timeout: int) -> None:
    script = f"""
new Promise((resolve) => {{
  const started = Date.now();
  const minimum = {int(minimum)};
  const timeout = {int(timeout) * 1000};
  const countImages = () => Array.from(document.querySelectorAll("img"))
    .filter((img) => {{
      const rect = img.getBoundingClientRect();
      const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
      return src && rect.width >= 50 && rect.height >= 50 && (img.naturalWidth || rect.width) >= 80;
    }}).length;
  const tick = () => {{
    if (countImages() >= minimum || Date.now() - started > timeout) {{
      resolve(true);
      return;
    }}
    setTimeout(tick, 250);
  }};
  tick();
}})
"""
    try:
        await tab.execute_script(script, return_by_value=True, await_promise=True)
    except Exception:
        return


async def extract_image_results(tab: Any, limit: int) -> list[dict[str, Any]]:
    script = f"""
(() => {{
  const limit = {int(limit)};
  const seen = new Set();
  const items = [];
  const images = Array.from(document.querySelectorAll("img"));

  for (const img of images) {{
    const rect = img.getBoundingClientRect();
    const thumbnailUrl = img.currentSrc || img.src || img.getAttribute("data-src") || "";
    const width = Math.round(img.naturalWidth || img.width || rect.width || 0);
    const height = Math.round(img.naturalHeight || img.height || rect.height || 0);
    const renderedWidth = Math.round(rect.width || 0);
    const renderedHeight = Math.round(rect.height || 0);
    const absoluteTop = Math.round(rect.top + window.scrollY);

    if (!thumbnailUrl || thumbnailUrl.startsWith("data:image/gif")) continue;
    if (width < 80 || height < 80 || renderedWidth < 50 || renderedHeight < 50) continue;
    if (absoluteTop < 140) continue;
    if (/\/logos\/doodles\/|googlelogo|\/images\/branding\//i.test(thumbnailUrl)) continue;
    if (seen.has(thumbnailUrl)) continue;

    const anchor = img.closest("a[href]");
    const pageUrl = anchor ? new URL(anchor.getAttribute("href"), location.href).href : "";
    const tile = img.closest("[data-ri], [data-attrid], div[jsname], div") || img.parentElement;
    const rawTitle = img.getAttribute("alt")
      || img.getAttribute("aria-label")
      || (anchor ? anchor.getAttribute("aria-label") : "")
      || (tile ? (tile.innerText || "").split("\\n")[0] : "")
      || "";
    if (rawTitle.trim().toLowerCase() === "google") continue;

    seen.add(thumbnailUrl);
    const marker = String(items.length + 1);
    img.dataset.imageSearchIndex = marker;

    items.push({{
      marker,
      title: rawTitle.trim(),
      thumbnail_url: thumbnailUrl,
      page_url: pageUrl,
      width,
      height,
      rendered_width: renderedWidth,
      rendered_height: renderedHeight,
      top: absoluteTop
    }});

    if (items.length >= limit) break;
  }}

  return items;
}})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    value = _response_value(response)
    return value if isinstance(value, list) else []


async def google_blocked(tab: Any) -> bool:
    script = r"""
(() => {
  const text = (document.body ? document.body.innerText : "").toLowerCase();
  return text.includes("unusual traffic")
    || text.includes("not a robot")
    || Boolean(document.querySelector("iframe[src*='recaptcha'], #captcha, .g-recaptcha"));
})()
"""
    try:
        response = await tab.execute_script(script, return_by_value=True)
    except Exception:
        return False
    return bool(_response_value(response))


async def save_image_result(
    tab: Any,
    raw: dict[str, Any],
    images_dir: Path,
    index: int,
    *,
    referer: str,
    quality: int,
    timeout: int,
) -> ImageResult:
    page_url = str(raw.get("page_url") or "")
    thumbnail_url = str(raw.get("thumbnail_url") or "")
    source_url = extract_imgres_url(page_url) or thumbnail_url
    title = str(raw.get("title") or "").strip()
    base_path = images_dir / f"image-{index:02d}"
    errors: list[str] = []

    for candidate_url in dedupe_urls([source_url, thumbnail_url]):
        if not candidate_url:
            continue
        try:
            path = await asyncio.to_thread(download_image, candidate_url, base_path, referer, timeout)
            return build_result(raw, index, title, source_url, thumbnail_url, page_url, path, "download")
        except Exception as exc:
            errors.append(f"{shorten_url(candidate_url)}: {exc}")

    screenshot_path = base_path.with_suffix(".png")
    captured = await screenshot_candidate(tab, str(raw.get("marker") or ""), screenshot_path, quality)
    if captured:
        error = "; ".join(errors)
        return build_result(raw, index, title, source_url, thumbnail_url, page_url, screenshot_path, "element-screenshot", error)

    raise RuntimeError(f"could not save image {index}: {'; '.join(errors) or 'no usable image URL'}")


def build_result(
    raw: dict[str, Any],
    index: int,
    title: str,
    source_url: str,
    thumbnail_url: str,
    page_url: str,
    path: Path,
    method: str,
    error: str = "",
) -> ImageResult:
    return ImageResult(
        index=index,
        title=title,
        source_url=metadata_url(source_url),
        thumbnail_url=metadata_url(thumbnail_url),
        page_url=page_url,
        width=coerce_int(raw.get("width")),
        height=coerce_int(raw.get("height")),
        rendered_width=coerce_int(raw.get("rendered_width")),
        rendered_height=coerce_int(raw.get("rendered_height")),
        path=str(path),
        method=method,
        error=error,
    )


def download_image(url: str, base_path: Path, referer: str, timeout: int) -> Path:
    if url.startswith("data:image/"):
        data, content_type = decode_data_image(url)
        extension = extension_for_content(content_type, data)
        path = base_path.with_suffix(extension)
        path.write_bytes(data)
        return path

    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": referer,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read()
            content_type = response.headers.get_content_type()
    except URLError as exc:
        raise RuntimeError(str(exc)) from exc

    if not data:
        raise RuntimeError("empty response")
    if content_type and not (content_type.startswith("image/") or looks_like_image(data)):
        raise RuntimeError(f"response was {content_type}, not an image")

    extension = extension_for_content(content_type, data, url)
    path = base_path.with_suffix(extension)
    path.write_bytes(data)
    return path


def decode_data_image(url: str) -> tuple[bytes, str]:
    header, encoded = url.split(",", 1)
    match = re.match(r"data:(image/[^;]+)", header)
    content_type = match.group(1) if match else "image/png"
    if ";base64" in header:
        return base64.b64decode(encoded), content_type
    return unquote_to_bytes(encoded), content_type


def metadata_url(url: str) -> str:
    if not url.startswith("data:image/"):
        return url
    try:
        data, content_type = decode_data_image(url)
    except Exception:
        return "data:image/*;<inline>"
    return f"data:{content_type};<inline {len(data)} bytes>"


async def screenshot_candidate(tab: Any, marker: str, path: Path, quality: int) -> bool:
    if not marker:
        return False
    selector = f'img[data-image-search-index="{css_escape(marker)}"]'
    try:
        element = await tab.query(selector, timeout=3, raise_exc=False)
        if not element:
            return False
        await element.take_screenshot(path, quality=quality)
        return True
    except Exception:
        return False


def image_output_to_json(output: ImageSearchOutput) -> dict[str, Any]:
    return {
        "query": output.query,
        "url": output.url,
        "screenshot": output.screenshot,
        "results_json": output.results_json,
        "results": [asdict(result) for result in output.results],
    }


def extract_imgres_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
    except ValueError:
        return ""
    values = params.get("imgurl") or []
    return str(values[0]) if values else ""


def extension_for_content(content_type: str | None, data: bytes, url: str = "") -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
        "image/svg+xml": ".svg",
    }
    if content_type in mapping:
        return mapping[content_type]

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg"}:
        return ".jpg" if suffix == ".jpeg" else suffix

    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.lstrip().startswith(b"<svg"):
        return ".svg"

    guessed = mimetypes.guess_extension(content_type or "")
    return guessed or ".img"


def looks_like_image(data: bytes) -> bool:
    return extension_for_content(None, data) != ".img"


def dedupe_urls(urls: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def shorten_url(url: str, limit: int = 100) -> str:
    if len(url) <= limit:
        return url
    return f"{url[:limit]}..."
