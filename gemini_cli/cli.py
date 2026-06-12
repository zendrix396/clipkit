from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.error import URLError
from urllib.parse import unquote_to_bytes
from urllib.request import Request, urlopen

from pydoll.constants import Key

from youtube_cli.browser import BrowserSettings, _response_value, open_browser


GEMINI_URL = "https://gemini.google.com/"
DEFAULT_OUTPUT = Path("gemini-cli-output")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
IMAGE_URL_COLLECTOR_JS = r"""
  const urlPattern = /(https?:\/\/[^\s"'<>\\)]+|blob:[^\s"'<>\\)]+|data:image\/[^\s"'<>]+)/g;
  const addUrl = (urls, value) => {
    if (!value) return;
    const text = String(value);
    if (/^(https?:|blob:|data:image\/)/.test(text)) urls.add(text);
    for (const match of text.matchAll(urlPattern)) urls.add(match[1]);
  };
  const addSrcset = (urls, value) => {
    if (!value) return;
    for (const part of String(value).split(",")) {
      addUrl(urls, part.trim().split(/\s+/)[0]);
    }
  };
  const collectUrls = (img) => {
    const urls = new Set();
    addUrl(urls, img.currentSrc || img.src || "");
    addSrcset(urls, img.getAttribute("srcset") || "");

    const nodes = [img];
    let node = img.parentElement;
    for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
      nodes.push(node);
    }

    const container = img.closest("message-content, user-query, model-response, mat-card, div");
    if (container) {
      nodes.push(...Array.from(container.querySelectorAll("a[href], img[src], source[srcset], picture, button, div[role='button']")));
    }

    for (const item of nodes) {
      if (!item || !item.getAttributeNames) continue;
      for (const attr of item.getAttributeNames()) {
        const value = item.getAttribute(attr) || "";
        if (/href|src|url|image|download|data/i.test(attr) || /https?:|blob:|data:image\//.test(value)) {
          addUrl(urls, value);
          if (attr.toLowerCase().includes("srcset")) addSrcset(urls, value);
        }
      }
    }

    return Array.from(urls);
  };
"""


@dataclass(frozen=True)
class GeminiImage:
    index: int
    path: str
    source: str
    width: int | None = None
    height: int | None = None
    mime: str = ""
    method: str = ""


@dataclass(frozen=True)
class GeminiImageOutput:
    prompt: str
    reference_images: list[str]
    output_dir: str
    screenshots: list[str]
    images: list[GeminiImage]
    metadata_json: str


@dataclass(frozen=True)
class GeminiLoginOutput:
    profile_dir: str
    output_dir: str
    screenshots: list[str]
    logged_in: bool
    metadata_json: str


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "login":
            output = asyncio.run(run_login(args))
            print(json.dumps(asdict(output), indent=2, ensure_ascii=False))
            return 0 if output.logged_in else 1
        if args.command == "image":
            output = asyncio.run(run_image(args))
            print(json.dumps(asdict(output), indent=2, ensure_ascii=False))
            return 0
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
        prog="gemini-cli",
        description="Use Pydoll to drive Gemini image generation and save generated images.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="Open visible Gemini sign-in and persist the Google session in a Chrome profile.")
    login.add_argument("--email", default=None)
    login.add_argument("--email-env", default="GEMINI_GOOGLE_EMAIL")
    login.add_argument("--password-env", default="GEMINI_GOOGLE_PASSWORD")
    login.add_argument("--no-fill-email", action="store_true")
    login.add_argument("--no-auto-password", action="store_true")
    login.add_argument("--out", default=None)
    login.add_argument("--manual-wait", type=int, default=600)
    login.add_argument("--after-email-wait", type=float, default=10.0)
    login.add_argument("--after-password-wait", type=float, default=8.0)
    add_browser_args(login)
    login.set_defaults(profile_dir="gemini-profile", headless=False, headless_new=False)

    image = subparsers.add_parser("image", help="Create an image in Gemini and save the generated output.")
    image.add_argument("prompt")
    image.add_argument(
        "--reference-image",
        "--input-image",
        action="append",
        default=[],
        dest="reference_images",
        help="Image file to upload as Gemini context before generating. Repeat for multiple references.",
    )
    image.add_argument("--email", default=None)
    image.add_argument("--password-env", default="GEMINI_GOOGLE_PASSWORD")
    image.add_argument("--email-env", default="GEMINI_GOOGLE_EMAIL")
    image.add_argument("--out", default=None)
    image.add_argument("--manual-wait", type=int, default=180)
    image.add_argument("--manual-password", action="store_true")
    image.add_argument("--after-email-wait", type=float, default=8.0)
    image.add_argument("--after-password-wait", type=float, default=8.0)
    image.add_argument("--generation-timeout", type=int, default=240)
    add_browser_args(image)

    return parser


def add_browser_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-dir", default="gemini-profile")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headless-new", action="store_true")
    parser.add_argument("--browser-binary", default=None)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--wait", type=float, default=2.0)
    parser.add_argument("--quality", type=int, default=90)


def settings_from_args(args: argparse.Namespace) -> BrowserSettings:
    return BrowserSettings(
        headless=bool(args.headless),
        headless_new=bool(args.headless_new),
        browser_binary=args.browser_binary,
        profile_dir=Path(args.profile_dir) if args.profile_dir else None,
        timeout=args.timeout,
        wait=args.wait,
        quality=args.quality,
    )


def resolve_reference_images(values: list[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Reference image does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"Reference image is not a file: {path}")
        paths.append(path)
    return paths


async def run_login(args: argparse.Namespace) -> GeminiLoginOutput:
    email = args.email or os.environ.get(args.email_env)
    password = os.environ.get(args.password_env)
    output_dir = Path(args.out) if args.out else timestamped_dir(DEFAULT_OUTPUT, "login")
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshots: list[str] = []

    settings = settings_from_args(args)
    async with open_browser(settings) as (_, tab):
        await safe_go_to(tab, GEMINI_URL, timeout=args.timeout)
        await asyncio.sleep(args.wait)
        screenshots.append(await save_screenshot(tab, output_dir, "01-opened", args.quality))

        if await sign_in_visible(tab):
            await click_top_right_sign_in(tab)
            await asyncio.sleep(2)

        if email and not args.no_fill_email:
            await fill_email(tab, email)
            screenshots.append(await save_screenshot(tab, output_dir, "02-email-submitted", args.quality))
            await asyncio.sleep(args.after_email_wait)

        if password and not args.no_auto_password:
            if await fill_password(tab, password):
                screenshots.append(await save_screenshot(tab, output_dir, "03-password-submitted", args.quality))
                await asyncio.sleep(args.after_password_wait)
            else:
                print("Password field was not found; complete login manually in the visible browser.")
        else:
            print("No password env available, or --no-auto-password was used.")

        print(f"Waiting up to {args.manual_wait}s for Gemini to become logged in. Profile: {args.profile_dir}")
        logged_in = await wait_for_logged_in_gemini(tab, timeout=args.manual_wait)
        screenshots.append(await save_screenshot(tab, output_dir, "04-login-result", args.quality))

    metadata_json = output_dir / "login.json"
    output = GeminiLoginOutput(
        profile_dir=str(Path(args.profile_dir).resolve()),
        output_dir=str(output_dir),
        screenshots=screenshots,
        logged_in=logged_in,
        metadata_json=str(metadata_json),
    )
    metadata_json.write_text(json.dumps(asdict(output), indent=2, ensure_ascii=False), encoding="utf-8")
    return output


async def run_image(args: argparse.Namespace) -> GeminiImageOutput:
    email = args.email or os.environ.get(args.email_env)
    password = os.environ.get(args.password_env)
    reference_images = resolve_reference_images(args.reference_images)

    output_dir = Path(args.out) if args.out else timestamped_dir(DEFAULT_OUTPUT, "image")
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshots: list[str] = []

    settings = settings_from_args(args)

    async with open_browser(settings) as (_, tab):
        await safe_go_to(tab, GEMINI_URL, timeout=args.timeout)
        await asyncio.sleep(args.wait)
        screenshots.append(await save_screenshot(tab, output_dir, "01-opened", args.quality))

        if not await logged_in_gemini_ready(tab, timeout=8):
            if not email:
                raise RuntimeError(
                    "Gemini is not logged in. Run `gemini-cli login --profile-dir "
                    f"{args.profile_dir}` first, or pass --email for the interactive login flow."
                )
            await ensure_logged_in(tab, email, password, output_dir, screenshots, args)
        await dismiss_gemini_overlays(tab)
        screenshots.append(await save_screenshot(tab, output_dir, "02-logged-in", args.quality))

        if reference_images:
            uploaded = await upload_reference_images(tab, reference_images)
            screenshots.append(await save_screenshot(tab, output_dir, "03-reference-uploaded", args.quality))
            if not uploaded:
                raise RuntimeError("Could not upload the reference image into Gemini")

        tool_selected = await select_create_image_tool(tab)
        screenshots.append(await save_screenshot(tab, output_dir, "04-tool-selected", args.quality))
        if not tool_selected:
            print("Create image tool was not found; continuing with an explicit image-generation prompt.")

        before_sources = await collect_image_sources(tab)
        submitted = await submit_prompt(tab, args.prompt)
        screenshots.append(await save_screenshot(tab, output_dir, "05-prompt-submitted", args.quality))
        if not submitted:
            raise RuntimeError("Could not find Gemini prompt input or send button")

        raw_images = await wait_for_generated_images(tab, before_sources, timeout=args.generation_timeout)
        screenshots.append(await save_screenshot(tab, output_dir, "06-result", args.quality))

        page_links = await collect_image_link_candidates(tab, [])
        images = await save_generated_images(tab, output_dir, raw_images, args.quality, page_links)
        if not images:
            write_debug_json(output_dir / "image-save-debug.json", raw_images, page_links)
            raise RuntimeError(
                "Gemini generated an image, but no generated image URL/source could be fetched. "
                f"Debug saved at {output_dir / 'image-save-debug.json'}"
            )

    metadata_json = output_dir / "result.json"
    output = GeminiImageOutput(
        prompt=args.prompt,
        reference_images=[str(path) for path in reference_images],
        output_dir=str(output_dir),
        screenshots=screenshots,
        images=images,
        metadata_json=str(metadata_json),
    )
    metadata_json.write_text(json.dumps(asdict(output), indent=2, ensure_ascii=False), encoding="utf-8")
    return output


async def ensure_logged_in(
    tab: Any,
    email: str,
    password: str | None,
    output_dir: Path,
    screenshots: list[str],
    args: argparse.Namespace,
) -> None:
    if await gemini_ready(tab, timeout=8) and not await sign_in_visible(tab):
        return

    if await sign_in_visible(tab):
        await click_top_right_sign_in(tab)
    await asyncio.sleep(2)

    if await fill_email(tab, email):
        screenshots.append(await save_screenshot(tab, output_dir, "login-email", args.quality))
        await asyncio.sleep(args.after_email_wait)

    if args.manual_password:
        print(f"Enter the password manually in the visible browser. Waiting up to {args.manual_wait}s.")
        screenshots.append(await save_screenshot(tab, output_dir, "login-password-manual", args.quality))
        if not await wait_until_not_password_page(tab, timeout=args.manual_wait):
            raise RuntimeError("Still on password/login page after manual password wait")
    elif password and await fill_password(tab, password):
        screenshots.append(await save_screenshot(tab, output_dir, "login-password-submitted", args.quality))
        await asyncio.sleep(args.after_password_wait)

    if await gemini_ready(tab, timeout=20) and not await sign_in_visible(tab):
        return

    await click_by_text(tab, ["continue to gemini", "use gemini", "try gemini", "continue"])
    await asyncio.sleep(3)
    if await gemini_ready(tab, timeout=12) and not await sign_in_visible(tab):
        return

    print(f"Manual verification may be required. Waiting up to {args.manual_wait}s in the visible browser.")
    screenshots.append(await save_screenshot(tab, output_dir, "login-manual-required", args.quality))
    for _ in range(max(1, args.manual_wait)):
        if await gemini_ready(tab, timeout=1) and not await sign_in_visible(tab):
            return
        await asyncio.sleep(1)
    else:
        raise RuntimeError("Gemini prompt did not become available after login/manual wait")


async def safe_go_to(tab: Any, url: str, timeout: int) -> None:
    try:
        await asyncio.wait_for(tab.go_to(url, timeout=timeout), timeout=timeout + 5)
    except asyncio.TimeoutError:
        print(f"Navigation timed out after {timeout}s; continuing with current page state.")


async def fill_email(tab: Any, email: str) -> bool:
    filled = await fill_visible_input(
        tab,
        [
            'input[type="email"]',
            'input[name="identifier"]',
            '#identifierId',
        ],
        email,
        timeout=12,
    )
    if not filled:
        return False
    await asyncio.sleep(0.5)
    await click_by_selector(tab, "#identifierNext button, #identifierNext")
    await click_by_text(tab, ["next"])
    return True


async def fill_password(tab: Any, password: str) -> bool:
    focused = await focus_visible_input(
        tab,
        [
            'input[type="password"]',
            'input[name="Passwd"]',
        ],
        timeout=20,
    )
    if not focused:
        return False
    await clear_focused_input(tab)
    await tab.keyboard.type_text(password, humanize=True, interval=0.06)
    await asyncio.sleep(0.5)
    await asyncio.sleep(0.5)
    await click_by_selector(tab, "#passwordNext button, #passwordNext")
    await click_by_text(tab, ["next"])
    return True


async def wait_until_not_password_page(tab: Any, timeout: int) -> bool:
    for _ in range(max(1, timeout)):
        if await gemini_ready(tab, timeout=1):
            return True
        password_visible = await visible_selector(tab, ['input[type="password"]', 'input[name="Passwd"]'])
        if not password_visible and not await sign_in_visible(tab):
            return True
        await asyncio.sleep(1)
    return False


async def visible_selector(tab: Any, selectors: list[str]) -> bool:
    selectors_json = json.dumps(selectors)
    script = f"""
(() => {{
  const selectors = {selectors_json};
  return selectors.some((selector) =>
    Array.from(document.querySelectorAll(selector)).some((el) => {{
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    }})
  );
}})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    return bool(_response_value(response))


async def fill_visible_input(
    tab: Any,
    selectors: list[str],
    value: str,
    *,
    timeout: int,
) -> bool:
    selectors_json = json.dumps(selectors)
    value_json = json.dumps(value)
    script = f"""
(() => {{
  const selectors = {selectors_json};
  const value = {value_json};
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
  const findInput = () => {{
    for (const selector of selectors) {{
      for (const input of Array.from(document.querySelectorAll(selector))) {{
        const rect = input.getBoundingClientRect();
        const style = window.getComputedStyle(input);
        if (
          rect.width > 0
          && rect.height > 0
          && style.visibility !== "hidden"
          && style.display !== "none"
          && !input.disabled
        ) return input;
      }}
    }}
    return null;
  }};
  const input = findInput();
  if (!input) return false;
  input.focus();
  setter.call(input, "");
  input.dispatchEvent(new Event("input", {{ bubbles: true }}));
  setter.call(input, value);
  input.dispatchEvent(new Event("input", {{ bubbles: true }}));
  input.dispatchEvent(new Event("change", {{ bubbles: true }}));
  return true;
}})()
"""
    for _ in range(max(1, timeout)):
        response = await tab.execute_script(script, return_by_value=True)
        if bool(_response_value(response)):
            return True
        await asyncio.sleep(1)
    return False


async def focus_visible_input(
    tab: Any,
    selectors: list[str],
    *,
    timeout: int,
) -> bool:
    selectors_json = json.dumps(selectors)
    script = f"""
(() => {{
  const selectors = {selectors_json};
  const findInput = () => {{
    for (const selector of selectors) {{
      for (const input of Array.from(document.querySelectorAll(selector))) {{
        const rect = input.getBoundingClientRect();
        const style = window.getComputedStyle(input);
        if (
          rect.width > 0
          && rect.height > 0
          && style.visibility !== "hidden"
          && style.display !== "none"
          && !input.disabled
        ) return input;
      }}
    }}
    return null;
  }};
  const input = findInput();
  if (!input) return false;
  input.focus();
  input.click();
  return document.activeElement === input;
}})()
"""
    for _ in range(max(1, timeout)):
        response = await tab.execute_script(script, return_by_value=True)
        if bool(_response_value(response)):
            return True
        await asyncio.sleep(1)
    return False


async def clear_focused_input(tab: Any) -> None:
    script = r"""
(() => {
  const input = document.activeElement;
  if (!input || !("value" in input)) return false;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
  setter.call(input, "");
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
})()
"""
    await tab.execute_script(script, return_by_value=True)


async def gemini_ready(tab: Any, timeout: int) -> bool:
    script = r"""
(() => {
  const selectors = [
    'rich-textarea [contenteditable="true"]',
    '[contenteditable="true"]',
    'textarea',
    '[role="textbox"]'
  ];
  return selectors.some((selector) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 80 && rect.height > 20;
  });
})()
"""
    for _ in range(max(1, timeout)):
        try:
            response = await tab.execute_script(script, return_by_value=True)
            if bool(_response_value(response)):
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def logged_in_gemini_ready(tab: Any, timeout: int) -> bool:
    for _ in range(max(1, timeout)):
        if await gemini_ready(tab, timeout=1) and not await sign_in_visible(tab):
            return True
        await asyncio.sleep(1)
    return False


async def wait_for_logged_in_gemini(tab: Any, timeout: int) -> bool:
    for _ in range(max(1, timeout)):
        await click_by_text(tab, ["continue to gemini", "use gemini", "try gemini", "continue"])
        if await logged_in_gemini_ready(tab, timeout=1):
            return True
        await asyncio.sleep(1)
    return False


async def sign_in_visible(tab: Any) -> bool:
    script = r"""
(() => {
  const candidates = Array.from(document.querySelectorAll('a, button, div[role="button"]'));
  return candidates.some((el) => {
    const rect = el.getBoundingClientRect();
    const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase();
    return text.includes("sign in") && rect.width > 0 && rect.height > 0;
  });
})()
"""
    try:
        response = await tab.execute_script(script, return_by_value=True)
        return bool(_response_value(response))
    except Exception:
        return False


async def select_create_image_tool(tab: Any) -> bool:
    for texts in (
        ["tools", "more tools"],
        ["create image", "image"],
    ):
        if await click_by_text(tab, texts):
            await asyncio.sleep(1.5)

    selected = await click_by_text(tab, ["create image", "image generation", "image"])
    if selected:
        await asyncio.sleep(1)
        return True

    return await enable_tool_by_script(tab)


async def upload_reference_images(tab: Any, paths: list[Path]) -> bool:
    await dismiss_gemini_overlays(tab)
    before_sources = await collect_image_sources(tab)

    for _ in range(3):
        await dismiss_gemini_overlays(tab)
        if await upload_files_via_menu(tab, paths):
            if await wait_for_reference_upload(tab, before_sources, timeout=12):
                return True

        if await accept_upload_agreement(tab):
            await asyncio.sleep(1)
            continue

        if await set_file_input_files(tab, paths):
            if await wait_for_reference_upload(tab, before_sources, timeout=12):
                return True

    return False


async def upload_files_via_menu(tab: Any, paths: list[Path]) -> bool:
    try:
        async with tab.expect_file_chooser([str(path) for path in paths]):
            if not await click_upload_tools_button_with_mouse(tab):
                return False
            await asyncio.sleep(1)
            if not await click_upload_files_menu_item_with_mouse(tab):
                return False
        return True
    except Exception:
        return False


async def set_file_input_files(tab: Any, paths: list[Path]) -> bool:
    inputs = await tab.query('input[type="file"]', timeout=2, find_all=True, raise_exc=False)
    if not inputs:
        return False
    input_list = inputs if isinstance(inputs, list) else [inputs]
    files = [str(path) for path in paths]
    for input_element in input_list:
        try:
            await input_element.set_input_files(files)
            return True
        except Exception:
            continue
    return False


async def click_attachment_button(tab: Any) -> bool:
    script = r"""
(() => {
  const needles = ["add files", "attach", "upload", "insert", "add image"];
  const elements = Array.from(document.querySelectorAll('button, div[role="button"], a, input[type="file"]'));
  const scored = elements
    .map((el) => {
      const rect = el.getBoundingClientRect();
      const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "") + " " + (el.title || "")).toLowerCase();
      const looksLikePlus = text.includes("add") || text.includes("attach") || text.includes("upload") || text.includes("file") || text.includes("image") || text.trim() === "+";
      return { el, rect, text, score: looksLikePlus ? 1 : 0 };
    })
    .filter((item) => item.score && item.rect.width > 0 && item.rect.height > 0)
    .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);

  const exact = scored.find((item) => needles.some((needle) => item.text.includes(needle))) || scored[0];
  if (!exact) return false;
  exact.el.click();
  return true;
})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    return bool(_response_value(response))


async def click_upload_tools_button(tab: Any) -> bool:
    script = r"""
(() => {
  const button = Array.from(document.querySelectorAll('button, div[role="button"]')).find((el) => {
    const rect = el.getBoundingClientRect();
    const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase();
    return text.includes("upload & tools") && rect.width > 0 && rect.height > 0;
  });
  if (!button) return false;
  button.click();
  return true;
})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    if bool(_response_value(response)):
        return True
    return await click_attachment_button(tab)


async def click_upload_tools_button_with_mouse(tab: Any) -> bool:
    rect = await upload_tools_rect(tab)
    if not rect:
        return await click_upload_tools_button(tab)
    await tab.mouse.click(rect["x"], rect["y"], humanize=True)
    return True


async def click_upload_files_menu_item(tab: Any) -> bool:
    script = r"""
(() => {
  const item = Array.from(document.querySelectorAll('button, div[role="menuitem"], button[role="menuitem"]')).find((el) => {
    const rect = el.getBoundingClientRect();
    const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase();
    return text.includes("upload files") && rect.width > 0 && rect.height > 0;
  });
  if (item) {
    item.click();
    return true;
  }

  const tool = Array.from(document.querySelectorAll('button, div[role="button"]')).find((el) => {
    const rect = el.getBoundingClientRect();
    const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase();
    return text.includes("upload & tools") && rect.width > 0 && rect.height > 0;
  });
  if (!tool) return false;
  const rect = tool.getBoundingClientRect();
  const target = document.elementFromPoint(rect.left + 115, rect.bottom + 18);
  const clickable = target && target.closest('button, div[role="menuitem"], button[role="menuitem"]');
  if (!clickable) return false;
  clickable.click();
  return true;
})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    if bool(_response_value(response)):
        return True
    return await click_by_text(tab, ["upload files"])


async def click_upload_files_menu_item_with_mouse(tab: Any) -> bool:
    rect = await wait_for_upload_files_rect(tab, timeout=5)
    if not rect:
        return await click_upload_files_menu_item(tab)
    await tab.mouse.click(rect["x"], rect["y"], humanize=True)
    return True


async def upload_tools_rect(tab: Any) -> dict[str, float] | None:
    script = r"""
(() => {
  const button = Array.from(document.querySelectorAll('button, div[role="button"]')).find((el) => {
    const rect = el.getBoundingClientRect();
    const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase();
    return text.includes("upload & tools") && rect.width > 0 && rect.height > 0;
  });
  if (!button) return null;
  const rect = button.getBoundingClientRect();
  return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    value = _response_value(response)
    return value if isinstance(value, dict) else None


async def wait_for_upload_files_rect(tab: Any, timeout: int) -> dict[str, float] | None:
    for _ in range(max(1, timeout * 4)):
        rect = await upload_files_rect(tab)
        if rect:
            return rect
        await asyncio.sleep(0.25)
    return None


async def upload_files_rect(tab: Any) -> dict[str, float] | None:
    script = r"""
(() => {
  const candidates = Array.from(document.querySelectorAll('button, div[role="menuitem"], button[role="menuitem"]'))
    .map((el) => {
      const rect = el.getBoundingClientRect();
      const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase();
      return { el, rect, text };
    })
    .filter((item) => item.text.includes("upload files") && item.rect.width > 0 && item.rect.height > 0)
    .sort((a, b) => a.rect.top - b.rect.top);

  if (candidates.length) {
    const rect = candidates[0].rect;
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }

  const tool = Array.from(document.querySelectorAll('button, div[role="button"]')).find((el) => {
    const rect = el.getBoundingClientRect();
    const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase();
    return text.includes("upload & tools") && rect.width > 0 && rect.height > 0;
  });
  if (!tool) return null;
  const rect = tool.getBoundingClientRect();
  return { x: rect.left + 116, y: rect.bottom + 18 };
})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    value = _response_value(response)
    return value if isinstance(value, dict) else None


async def accept_upload_agreement(tab: Any) -> bool:
    script = r"""
(() => {
  const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
  const button = buttons.find((el) => {
    const rect = el.getBoundingClientRect();
    const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase().trim();
    return text.includes("agree") && rect.width > 0 && rect.height > 0;
  });
  if (!button) return false;
  button.click();
  return true;
})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    return bool(_response_value(response))


async def wait_for_reference_upload(tab: Any, before_sources: list[str], timeout: int = 90) -> bool:
    before = set(before_sources)
    for _ in range(max(1, timeout // 2)):
        candidates = await collect_reference_image_candidates(tab, before)
        if candidates:
            return True
        await asyncio.sleep(2)
    return False


async def collect_reference_image_candidates(tab: Any, before: set[str]) -> list[dict[str, Any]]:
    before_json = json.dumps(sorted(before))
    script = f"""
(() => {{
  const before = new Set({before_json});
  return Array.from(document.images)
    .map((img, index) => {{
      const rect = img.getBoundingClientRect();
      const src = img.currentSrc || img.src || "";
      return {{
        index,
        src,
        width: img.naturalWidth || Math.round(rect.width),
        height: img.naturalHeight || Math.round(rect.height),
        displayWidth: Math.round(rect.width),
        displayHeight: Math.round(rect.height),
        alt: img.alt || ""
      }};
    }})
    .filter((img) =>
      img.src
      && !before.has(img.src)
      && img.width >= 64
      && img.height >= 64
      && img.displayWidth >= 32
      && img.displayHeight >= 32
    );
}})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    value = _response_value(response)
    return value if isinstance(value, list) else []


async def dismiss_gemini_overlays(tab: Any) -> None:
    for _ in range(3):
        clicked = await click_by_text(tab, ["got it", "dismiss", "not now", "no thanks", "i agree", "continue"])
        if not clicked:
            return
        await asyncio.sleep(1)


async def submit_prompt(tab: Any, prompt: str) -> bool:
    element = await first_element(
        tab,
        [
            'rich-textarea [contenteditable="true"]',
            '[contenteditable="true"][role="textbox"]',
            '[contenteditable="true"]',
            'textarea',
            '[role="textbox"]',
        ],
        timeout=30,
    )
    if not element:
        return False

    final_prompt = prompt
    if "image" not in prompt.lower():
        final_prompt = f"Create an image: {prompt}"

    await element.click(humanize=True)
    await element.type_text(final_prompt, humanize=True)
    await asyncio.sleep(0.5)

    if await click_send_button(tab):
        return True

    await tab.keyboard.press(Key.ENTER)
    return True


async def wait_for_generated_images(
    tab: Any,
    before_sources: list[str],
    *,
    timeout: int,
) -> list[dict[str, Any]]:
    before = set(before_sources)
    for _ in range(max(1, timeout // 3)):
        images = await collect_image_candidates(tab, before)
        if images:
            return images
        await asyncio.sleep(3)
    return []


async def collect_image_candidates(tab: Any, before: set[str]) -> list[dict[str, Any]]:
    before_json = json.dumps(sorted(before))
    script = f"""
(() => {{
  const before = new Set({before_json});
{IMAGE_URL_COLLECTOR_JS}
  return Array.from(document.images)
    .map((img, index) => {{
      const rect = img.getBoundingClientRect();
      const src = img.currentSrc || img.src || "";
      return {{
        index,
        src,
        width: img.naturalWidth || Math.round(rect.width),
        height: img.naturalHeight || Math.round(rect.height),
        displayWidth: Math.round(rect.width),
        displayHeight: Math.round(rect.height),
        top: Math.round(rect.top),
        alt: img.alt || "",
        urls: collectUrls(img)
      }};
    }})
    .filter((img) =>
      img.src
      && !before.has(img.src)
      && img.width >= 256
      && img.height >= 256
      && img.displayWidth >= 160
      && img.displayHeight >= 120
    );
}})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    value = _response_value(response)
    return value if isinstance(value, list) else []


async def save_generated_images(
    tab: Any,
    output_dir: Path,
    raw_images: list[dict[str, Any]],
    quality: int,
    link_sources: list[str] | None = None,
) -> list[GeminiImage]:
    images: list[GeminiImage] = []
    for index, raw in enumerate(raw_images, start=1):
        source = str(raw.get("src") or "")
        extension = extension_for_source(source)
        image_path = output_dir / f"gemini-image-{index}{extension}"

        fetched = await fetch_best_image_data(tab, raw, link_sources or [])
        if fetched and fetched.get("data"):
            data = base64.b64decode(str(fetched["data"]))
            mime = str(fetched.get("mime") or "")
            source = str(fetched.get("source") or source)
            image_path = image_path.with_suffix(extension_for_mime(mime) or image_path.suffix)
            image_path.write_bytes(data)
            width, height = image_dimensions(image_path)
            images.append(
                GeminiImage(
                    index=index,
                    path=str(image_path),
                    source=source,
                    width=width or int(raw.get("width") or 0) or None,
                    height=height or int(raw.get("height") or 0) or None,
                    mime=mime,
                    method="image-link",
                )
            )
            continue

    return images


async def fetch_best_image_data(tab: Any, raw: dict[str, Any], link_sources: list[str]) -> dict[str, str] | None:
    best: dict[str, str] | None = None
    best_size = 0
    for source in candidate_image_sources(raw, link_sources):
        if source.startswith("data:image/"):
            fetched = await asyncio.to_thread(fetch_image_data_direct, source)
        else:
            fetched = await fetch_image_data(tab, source)
        if not fetched or not fetched.get("data"):
            fetched = await fetch_image_data_from_canvas(tab, source)
        if not fetched or not fetched.get("data"):
            fetched = await asyncio.to_thread(fetch_image_data_direct, source)
        if not fetched or not fetched.get("data"):
            continue
        mime = str(fetched.get("mime") or "")
        if mime and not mime.startswith("image/"):
            continue
        size = len(str(fetched.get("data") or ""))
        if size > best_size:
            fetched["source"] = source
            best = fetched
            best_size = size
    return best


def candidate_image_sources(raw: dict[str, Any], link_sources: list[str]) -> list[str]:
    seen = set()
    sources: list[str] = []
    values = raw.get("urls")
    if isinstance(values, list):
        candidates = [str(value) for value in values]
    else:
        candidates = []
    candidates = list(link_sources) + candidates
    candidates.append(str(raw.get("src") or ""))

    def score(source: str) -> tuple[int, int]:
        if source.startswith("blob:"):
            return (0, -len(source))
        if source.startswith("https://") or source.startswith("http://"):
            return (1, -len(source))
        if source.startswith("data:image/"):
            return (2, -len(source))
        return (3, -len(source))

    for source in sorted(candidates, key=score):
        if not source or source in seen:
            continue
        if not source.startswith(("https://", "http://", "blob:", "data:image/")):
            continue
        seen.add(source)
        sources.append(source)
    return sources


def write_debug_json(path: Path, raw_images: list[dict[str, Any]], page_links: list[str]) -> None:
    payload = {
        "page_links": page_links,
        "raw_images": [
            {
                "src": item.get("src"),
                "width": item.get("width"),
                "height": item.get("height"),
                "displayWidth": item.get("displayWidth"),
                "displayHeight": item.get("displayHeight"),
                "alt": item.get("alt"),
                "urls": item.get("urls"),
            }
            for item in raw_images
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


async def collect_image_link_candidates(tab: Any, before_links: list[str]) -> list[str]:
    before_json = json.dumps(sorted(before_links))
    script = f"""
(() => {{
  const before = new Set({before_json});
  const found = [];
  const seen = new Set();
  const add = (url, label = "") => {{
    if (!url || seen.has(url) || before.has(url)) return;
    if (!/^(https?:|blob:|data:image\\/)/.test(url)) return;
    seen.add(url);
    found.push({{ url, label }});
  }};

  for (const anchor of Array.from(document.querySelectorAll("a[href]"))) {{
    const href = anchor.href || anchor.getAttribute("href") || "";
    const label = [
      anchor.textContent || "",
      anchor.getAttribute("aria-label") || "",
      anchor.getAttribute("title") || "",
      anchor.getAttribute("download") || ""
    ].join(" ").toLowerCase();
    if (
      label.includes("download")
      || label.includes("full-resolution")
      || label.includes("full resolution")
      || label.includes("image")
      || /\\.(png|jpe?g|webp|avif)(\\?|$)/i.test(href)
    ) {{
      add(href, label);
    }}
  }}

  const text = document.body ? document.body.innerText : "";
  const matches = text.match(/https?:\\/\\/[^\\s"'<>]+/g) || [];
  for (const url of matches) {{
    if (/\\.(png|jpe?g|webp|avif)(\\?|$)/i.test(url) || /image|download|usercontent|google/i.test(url)) {{
      add(url, "text-url");
    }}
  }}

  return found
    .sort((a, b) => {{
      const score = (item) => (
        (item.label.includes("download") ? 0 : 10)
        + (item.label.includes("full-resolution") || item.label.includes("full resolution") ? 0 : 5)
        + (item.label.includes("image") ? 0 : 2)
      );
      return score(a) - score(b);
    }})
    .map((item) => item.url);
}})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    value = _response_value(response)
    return value if isinstance(value, list) else []


async def fetch_image_data(tab: Any, source: str) -> dict[str, str] | None:
    if not source:
        return None
    source_json = json.dumps(source)
    script = f"""
(async () => {{
  const src = {source_json};
  try {{
    const response = await fetch(src);
    const blob = await response.blob();
    const data = await new Promise((resolve, reject) => {{
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    }});
    return {{ data, mime: blob.type || "" }};
  }} catch (error) {{
    return {{ error: String(error && error.message || error) }};
  }}
}})()
"""
    response = await tab.execute_script(
        script,
        return_by_value=True,
        await_promise=True,
    )
    value = _response_value(response)
    return value if isinstance(value, dict) else None


async def fetch_image_data_from_canvas(tab: Any, source: str) -> dict[str, str] | None:
    if not source:
        return None
    source_json = json.dumps(source)
    script = f"""
(async () => {{
  const src = {source_json};
  try {{
    const img = Array.from(document.images).find((candidate) => (
      candidate.currentSrc || candidate.src || ""
    ) === src);
    if (!img) return {{ error: "image element not found" }};
    if (!img.complete) {{
      await new Promise((resolve, reject) => {{
        img.addEventListener("load", resolve, {{ once: true }});
        img.addEventListener("error", reject, {{ once: true }});
      }});
    }}
    const width = img.naturalWidth || img.width;
    const height = img.naturalHeight || img.height;
    if (!width || !height) return {{ error: "image has no dimensions" }};
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    context.drawImage(img, 0, 0, width, height);
    const dataUrl = canvas.toDataURL("image/png");
    return {{
      data: dataUrl.split(",")[1] || "",
      mime: "image/png"
    }};
  }} catch (error) {{
    return {{ error: String(error && error.message || error) }};
  }}
}})()
"""
    response = await tab.execute_script(
        script,
        return_by_value=True,
        await_promise=True,
    )
    value = _response_value(response)
    return value if isinstance(value, dict) else None


def fetch_image_data_direct(source: str) -> dict[str, str] | None:
    if source.startswith("data:image/"):
        try:
            data, mime = decode_data_image_url(source)
        except (ValueError, OSError):
            return None
        return {"data": base64.b64encode(data).decode("ascii"), "mime": mime}

    if not source.startswith(("http://", "https://")):
        return None

    request = Request(
        source,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": GEMINI_URL,
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            data = response.read()
            content_type = response.headers.get_content_type()
    except (OSError, URLError):
        return None

    if not data:
        return None
    mime = image_mime_from_bytes(data, content_type)
    if not mime.startswith("image/"):
        return None
    return {"data": base64.b64encode(data).decode("ascii"), "mime": mime}


def decode_data_image_url(source: str) -> tuple[bytes, str]:
    header, payload = source.split(",", 1)
    mime = header[5:].split(";", 1)[0] or "image/png"
    if ";base64" in header:
        return base64.b64decode(payload), mime
    return unquote_to_bytes(payload), mime


async def screenshot_image_element(tab: Any, raw: dict[str, Any], path: Path, quality: int) -> bool:
    source = str(raw.get("src") or "")
    marker = await mark_image_by_source(tab, source)
    if not marker:
        return False
    element = await tab.query(f'img[data-gemini-cli-image="{marker}"]', timeout=5, raise_exc=False)
    if not element:
        return False
    await element.take_screenshot(path, quality=quality)
    return True


async def mark_image_by_source(tab: Any, source: str) -> str:
    source_json = json.dumps(source)
    script = f"""
(() => {{
  const src = {source_json};
  const marker = String(Date.now());
  const image = Array.from(document.images).find((img) => (img.currentSrc || img.src || "") === src);
  if (!image) return "";
  image.setAttribute("data-gemini-cli-image", marker);
  return marker;
}})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    return str(_response_value(response) or "")


async def collect_image_sources(tab: Any) -> list[str]:
    script = r"""
(() => Array.from(document.images)
  .map((img) => img.currentSrc || img.src || "")
  .filter(Boolean))()
"""
    response = await tab.execute_script(script, return_by_value=True)
    value = _response_value(response)
    return value if isinstance(value, list) else []


async def click_send_button(tab: Any) -> bool:
    script = r"""
(() => {
  const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
  const button = buttons.find((el) => {
    const label = `${el.getAttribute("aria-label") || ""} ${el.textContent || ""}`.toLowerCase();
    if (!/(send|submit|run)/.test(label)) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && !el.disabled;
  });
  if (!button) return false;
  button.click();
  return true;
})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    return bool(_response_value(response))


async def click_by_selector(tab: Any, selector: str) -> bool:
    selector_json = json.dumps(selector)
    script = f"""
(() => {{
  const selector = {selector_json};
  const el = document.querySelector(selector);
  if (!el) return false;
  el.click();
  return true;
}})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    return bool(_response_value(response))


async def click_top_right_sign_in(tab: Any) -> bool:
    script = r"""
(() => {
  const candidates = Array.from(document.querySelectorAll('a, button, div[role="button"]'))
    .map((el) => {
      const rect = el.getBoundingClientRect();
      const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase();
      return { el, rect, text };
    })
    .filter((item) =>
      item.text.includes("sign in")
      && item.rect.width > 0
      && item.rect.height > 0
    )
    .sort((a, b) => (b.rect.right - a.rect.right) || (a.rect.top - b.rect.top));

  if (!candidates.length) return false;
  candidates[0].el.click();
  return true;
})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    if bool(_response_value(response)):
        return True
    return await click_by_text(tab, ["sign in"])


async def click_by_text(tab: Any, needles: list[str]) -> bool:
    needles_json = json.dumps(needles)
    script = f"""
(() => {{
  const needles = {needles_json};
  const lowered = needles.map((text) => String(text).toLowerCase());
  const elements = Array.from(document.querySelectorAll(
    'button, a, div[role="button"], div[role="menuitem"], mat-option, li, span'
  ));
  const candidate = elements.find((el) => {{
    const text = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase().trim();
    if (!text || !lowered.some((needle) => text.includes(needle))) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }});
  if (!candidate) return false;
  candidate.click();
  return true;
}})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    return bool(_response_value(response))


async def enable_tool_by_script(tab: Any) -> bool:
    script = r"""
(() => {
  const candidates = Array.from(document.querySelectorAll('*')).filter((el) => {
    const text = `${el.getAttribute("aria-label") || ""} ${el.textContent || ""}`.toLowerCase();
    return text.includes("create image") || text.includes("image generation");
  });
  const el = candidates.find((node) => {
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
  if (!el) return false;
  el.click();
  return true;
})()
"""
    response = await tab.execute_script(script, return_by_value=True)
    return bool(_response_value(response))


async def first_element(tab: Any, selectors: list[str], timeout: int) -> Any:
    for _ in range(max(1, timeout)):
        for selector in selectors:
            element = await tab.query(selector, timeout=0, raise_exc=False)
            if element:
                return element
        await asyncio.sleep(1)
    return None


async def save_screenshot(tab: Any, output_dir: Path, label: str, quality: int) -> str:
    path = output_dir / f"{label}.png"
    await tab.take_screenshot(path, quality=quality)
    return str(path)


def timestamped_dir(parent: Path, prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return parent / f"{prefix}-{stamp}"


def extension_for_source(source: str) -> str:
    lowered = source.lower().split("?", 1)[0]
    for extension in (".png", ".jpg", ".jpeg", ".webp"):
        if lowered.endswith(extension):
            return extension
    return ".png"


def extension_for_mime(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }.get(mime.lower(), "")


def newest_download(download_dir: Path) -> Path | None:
    files = [
        path
        for path in download_dir.iterdir()
        if path.is_file() and not path.name.endswith(".crdownload")
    ]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def normalized_image_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".avif"}:
        return ".jpg" if suffix == ".jpeg" else suffix

    try:
        data = path.read_bytes()[:32]
    except OSError:
        return ".png"

    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:12] == b"ftypavif":
        return ".avif"
    return ".png"


def mime_for_path(path: Path) -> str:
    suffix = normalized_image_suffix(path)
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".webp": "image/webp",
        ".avif": "image/avif",
    }.get(suffix, "")


def image_mime_from_bytes(data: bytes, content_type: str = "") -> str:
    if content_type.startswith("image/"):
        return content_type
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] == b"ftypavif":
        return "image/avif"
    return content_type or ""


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        data = path.read_bytes()
    except OSError:
        return None, None

    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")

    if data.startswith(b"\xff\xd8\xff"):
        return jpeg_dimensions(data)

    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return webp_dimensions(data)

    return None, None


def jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            return width, height
        index += segment_length
    return None, None


def webp_dimensions(data: bytes) -> tuple[int | None, int | None]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None, None
