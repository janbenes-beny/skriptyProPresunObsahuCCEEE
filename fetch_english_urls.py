import argparse
import asyncio
import json
import sys
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
except ModuleNotFoundError:  # pragma: no cover - runtime environment detail
    print(
        "This script requires the 'playwright' package.\n"
        "Install it with:\n"
        "  pip install playwright\n"
        "  playwright install chromium"
    )
    sys.exit(1)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "newsUrl-links.json"
DEFAULT_OUTPUT = BASE_DIR / "newsUrl-links-en.json"

# Primary selector: the <a> that leads to the EN translation
# Example:
# <a href="https://www.ccee.eu/.../?lang=en" class="wpml-ls-link">...</a>
LANG_SELECTOR_PRIMARY = "a.wpml-ls-link[href*='?lang=en']"

# Fallback selector: any WPML language link (first match)
LANG_SELECTOR_FALLBACK = "a.wpml-ls-link"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open each CCEE news URL, click the EN language switcher, "
            "and store resulting English URLs into a JSON file."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Path to input JSON with source URLs (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Path to output JSON with English URLs (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="If set, process only the first N URLs (useful for testing).",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run browser with UI (not headless) for debugging.",
    )
    return parser.parse_args()


def load_urls(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array of URLs in {path}, got {type(data).__name__}")

    urls: list[str] = []
    for idx, item in enumerate(data):
        if not isinstance(item, str):
            raise ValueError(f"Item at index {idx} in {path} is not a string: {item!r}")
        urls.append(item)

    return urls


def save_urls(urls: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(urls, f, ensure_ascii=False, indent=2)
    print(f"\nSaved {len(urls)} URLs to {path}")


async def process_single_url(
    page,
    url: str,
    index: int,
    total: int,
) -> str:
    prefix = f"[{index + 1}/{total}]"
    print(f"{prefix} Opening: {url}")

    try:
        await page.goto(url, wait_until="networkidle")
    except Exception as exc:  # noqa: BLE001
        print(f"{prefix} ERROR navigating to URL: {exc}")
        # Fallback: keep original URL so we don't lose alignment
        return url

    # Give the page a short moment to settle
    await page.wait_for_timeout(1000)

    # Try primary selector first
    try:
        await page.wait_for_selector(LANG_SELECTOR_PRIMARY, timeout=10_000)
        await page.click(LANG_SELECTOR_PRIMARY)
        print(f"{prefix} Clicked EN switcher (primary selector).")
    except PlaywrightTimeoutError:
        print(f"{prefix} Primary selector not found, trying fallback...")
        try:
            await page.wait_for_selector(LANG_SELECTOR_FALLBACK, timeout=5_000)
            await page.click(LANG_SELECTOR_FALLBACK)
            print(f"{prefix} Clicked EN switcher (fallback selector).")
        except PlaywrightTimeoutError:
            print(f"{prefix} ERROR: EN language switcher not found, keeping original URL.")
            return url
        except Exception as exc:  # noqa: BLE001
            print(f"{prefix} ERROR clicking EN switcher (fallback): {exc}")
            return url
    except Exception as exc:  # noqa: BLE001
        print(f"{prefix} ERROR clicking EN switcher (primary): {exc}")
        return url

    # Wait for navigation to finish and read the new URL
    try:
        await page.wait_for_load_state("networkidle")
    except Exception as exc:  # noqa: BLE001
        print(f"{prefix} WARNING: Load state 'networkidle' not reached: {exc}")

    new_url = page.url
    print(f"{prefix} English URL: {new_url}")
    return new_url


async def main_async() -> None:
    args = parse_args()

    try:
        urls = load_urls(args.input)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR loading input URLs: {exc}")
        sys.exit(1)

    if args.limit is not None and args.limit > 0:
        urls_to_process = urls[: args.limit]
    else:
        urls_to_process = urls

    total = len(urls_to_process)
    if total == 0:
        print("No URLs to process.")
        return

    print(f"Loaded {len(urls)} URLs, processing {total} of them.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headful)
        page = await browser.new_page()

        results: list[str] = []
        for idx, url in enumerate(urls_to_process):
            english_url = await process_single_url(page, url, idx, total)
            results.append(english_url)

        await browser.close()

    save_urls(results, args.output)


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")


if __name__ == "__main__":
    main()

