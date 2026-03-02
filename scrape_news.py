"""
CCEE News Scraper - stahuje články z URL v newsUrl-links.json,
extrahuje topic, title, annotation, content, obrázky a soubory.
Ukládá do složky News/{id}/ s article.json a podsložkami Images/ a Files/.
"""

import asyncio
import json
import logging
import os
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

# Konfigurace
SCRIPT_DIR = Path(__file__).resolve().parent
URLS_JSON = SCRIPT_DIR / "newsUrl-links.json"
NEWS_DIR = SCRIPT_DIR / "News"
BASE_URL = "https://www.ccee.eu"

# Timeout pro načtení stránky a stahování
PAGE_TIMEOUT_MS = 30000
DOWNLOAD_TIMEOUT_S = 30
MAX_PAGE_LOAD_RETRIES = 3
PAGE_RETRY_DELAY_S = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def resolve_url(url: str) -> str:
    """Převede relativní URL na absolutní."""
    if not url or url.startswith("data:"):
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE_URL + url
    if not url.startswith("http"):
        return BASE_URL.rstrip("/") + "/" + url.lstrip("/")
    return url


def get_extension_from_url(url: str, default: str = ".jpg") -> str:
    """Vrátí příponu souboru z URL (včetně tečky) nebo default."""
    path = urllib.parse.urlsplit(url).path
    if not path:
        return default
    base = path.split("?")[0]
    if "." in base:
        ext = "." + base.rsplit(".", 1)[-1].lower()
        if len(ext) <= 5 and ext.replace(".", "").isalnum():
            return ext
    return default


def download_file(url: str, dest_path: Path) -> bool:
    """Stáhne soubor z URL do dest_path. Vrátí True při úspěchu."""
    url = resolve_url(url)
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CCEE-News-Scraper/1.0"})
        # Některé Windows Python distribuce nemají správně dostupný trust store.
        # Pro scraping proto povolíme fallback bez validace certifikátu.
        ssl_ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S, context=ssl_ctx) as resp:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(resp.read())
        return True
    except Exception as e:
        logger.warning("Stažení selhalo %s -> %s: %s", url[:60], dest_path, e)
        return False


async def scrape_article(page, url: str, article_id: int) -> Optional[dict]:
    """
    Načte stránku, vytáhne data a stáhne obrázky/soubory.
    Vrátí slovník pro article.json nebo None při fatální chybě.
    """
    article_dir = NEWS_DIR / str(article_id)
    images_dir = article_dir / "Images"
    files_dir = article_dir / "Files"
    ensure_dir(article_dir)
    ensure_dir(images_dir)
    ensure_dir(files_dir)

    loaded = False
    for attempt in range(1, MAX_PAGE_LOAD_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            # Počkáme na hlavní obsah, aby se načetly lazy obrázky
            try:
                await page.wait_for_selector(".entry-content-post", timeout=10000)
            except Exception:
                pass
            loaded = True
            break
        except PlaywrightTimeoutError as e:
            logger.warning(
                "Timeout nacitani ID=%s, pokus %s/%s: %s",
                article_id,
                attempt,
                MAX_PAGE_LOAD_RETRIES,
                e,
            )
        except Exception as e:
            logger.warning(
                "Chyba nacitani ID=%s, pokus %s/%s: %s",
                article_id,
                attempt,
                MAX_PAGE_LOAD_RETRIES,
                e,
            )

        if attempt < MAX_PAGE_LOAD_RETRIES:
            await asyncio.sleep(PAGE_RETRY_DELAY_S)

    if not loaded:
        logger.error(
            "Načtení stránky ID=%s selhalo i po %s pokusech: %s",
            article_id,
            MAX_PAGE_LOAD_RETRIES,
            url,
        )
        return None

    async def text_of(selector: str) -> str:
        try:
            el = page.locator(selector).first
            if await el.count() == 0:
                return ""
            return (await el.inner_text() or "").strip()
        except Exception:
            return ""

    async def attr_of(selector: str, attr: str) -> str:
        try:
            el = page.locator(selector).first
            if await el.count() == 0:
                return ""
            return (await el.get_attribute(attr) or "").strip()
        except Exception:
            return ""

    topic = await text_of(".seed_wp_starter_so_occhiello")
    title = await text_of(".entry-title-post")
    annotation = await text_of(".seed_wp_starter_so_sottotitolo")

    content_html = ""
    try:
        el = page.locator(".entry-content-post").first
        if await el.count() > 0:
            content_html = (await el.inner_html() or "").strip()
            # Odebereme galerii z textu obsahu - obrazky resime zvlast.
            content_html = re.sub(
                r"<[^>]*class=['\"][^'\"]*\brl-gallery-container\b[^'\"]*['\"][^>]*>.*?</[^>]+>",
                "",
                content_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
    except Exception:
        pass

    # Hlavní obrázek (featured image z hlavičky článku)
    main_img_src = await attr_of(
        "img.attachment-post-thumbnail.size-post-thumbnail.wp-post-image", "src"
    )
    main_image_path = ""
    if main_img_src:
        ext = get_extension_from_url(main_img_src, ".jpg")
        main_filename = f"MainImage-{article_id}{ext}"
        main_path = article_dir / main_filename
        if download_file(main_img_src, main_path):
            main_image_path = main_filename

    # Obrázky v obsahu (všechny img uvnitř .entry-content-post)
    content_images = []
    try:
        imgs = await page.locator(".entry-content-post img").evaluate_all(
            """els =>
                els
                  .filter(el => !el.closest('ul.wpba-attachment-list.unstyled'))
                  .map(el => el.getAttribute('src') || '')
            """
        )
        imgs = [s for s in imgs if s and not s.startswith("data:")]
        for i, src in enumerate(imgs, 1):
            ext = get_extension_from_url(src, ".jpg")
            name = f"image_{i}{ext}"
            path = images_dir / name
            if download_file(src, path):
                content_images.append(f"Images/{name}")
    except Exception as e:
        logger.warning("Extrakce obrázků z obsahu ID=%s: %s", article_id, e)

    # Soubory z #wpba_attachment_list / .wpba_attachment_list (odkazy v <a href="...">)
    file_paths = []
    try:
        list_el = page.locator("#wpba_attachment_list, .wpba_attachment_list").first
        if await list_el.count() > 0:
            hrefs = await list_el.locator("a[href]").evaluate_all(
                """els => els.map(el => el.getAttribute('href') || '').filter(Boolean)"""
            )
            seen = set()
            for i, href in enumerate(hrefs, 1):
                href = href.strip()
                if not href or href in seen:
                    continue
                seen.add(href)
                ext = get_extension_from_url(href, ".bin")
                # Použijeme název z URL nebo file_N
                path_part = urllib.parse.urlsplit(href).path
                if path_part and "/" in path_part:
                    suggested = path_part.rsplit("/", 1)[-1].split("?")[0]
                    if suggested and "." in suggested:
                        name = suggested
                    else:
                        name = f"file_{i}{ext}"
                else:
                    name = f"file_{i}{ext}"
                # sanitize filename
                name = re.sub(r'[<>:"/\\|?*]', "_", name)[:200] or f"file_{i}{ext}"
                dest = files_dir / name
                if download_file(href, dest):
                    file_paths.append(f"Files/{name}")
    except Exception as e:
        logger.warning("Extrakce souborů wpba_attachment_list ID=%s: %s", article_id, e)

    return {
        "Id": article_id,
        "Url": url,
        "Topic": topic,
        "Title": title,
        "Annotation": annotation,
        "Content": content_html,
        "MainImage": main_image_path,
        "ContentImages": content_images,
        "Files": file_paths,
    }


def save_article_json(article_dir: Path, data: dict) -> None:
    path = article_dir / "article.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


async def main() -> None:
    if not URLS_JSON.exists():
        logger.error("Soubor %s nenalezen.", URLS_JSON)
        return

    with open(URLS_JSON, "r", encoding="utf-8") as f:
        urls = json.load(f)

    if not isinstance(urls, list):
        logger.error("JSON musí obsahovat pole URL.")
        return

    ensure_dir(NEWS_DIR)
    ok = 0
    failed = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()

        for index, url in enumerate(urls):
            article_id = index + 1

            if not url or not isinstance(url, str):
                logger.warning("Přeskočeno ID=%s (neplatná URL)", article_id)
                failed += 1
                continue
            url = url.strip()
            if not url:
                failed += 1
                continue

            logger.info("Zpracovávám ID=%s: %s", article_id, url[:60])
            try:
                data = await scrape_article(page, url, article_id)
                if data is not None:
                    article_dir = NEWS_DIR / str(article_id)
                    save_article_json(article_dir, data)
                    ok += 1
                else:
                    failed += 1
            except Exception as e:
                logger.exception("Chyba u ID=%s: %s", article_id, e)
                failed += 1

        await browser.close()

    logger.info("Hotovo. Zpracováno: %s, chyby/přeskočeno: %s", ok, failed)


if __name__ == "__main__":
    asyncio.run(main())
