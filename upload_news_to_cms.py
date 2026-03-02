import argparse
import json
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import Page, TimeoutError, sync_playwright


TENANT_URL = "https://cmshost01.dmpagency.cz/cs/Core/TenantSelector/Set?tenantId=b9351f22-e573-41cc-904c-33fb6fc8f029"
LOGIN_URL = "https://cmshost01.dmpagency.cz/admin"
NEWS_INDEX_URL = "https://cmshost01.dmpagency.cz/Core/NewsAdmin/News/Index"

DEFAULT_EMAIL = "plnic.novinek@dmpagency.cz"
DEFAULT_PASSWORD = "89CsKMUbsZxuRY7."


@dataclass
class Article:
    folder: Path
    title: str
    annotation: str
    content_html: str
    main_image: Optional[Path]
    content_images: List[Path]
    files: List[Path]


class _StripLinksAndImagesParser(HTMLParser):
    """
    Zachova puvodni HTML strukturu (p, div, span, strong, ...),
    ale odstrani tagy <a> a <img> (text uvnitr <a> ponecha).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        tag_lower = tag.lower()
        if tag_lower in ("a", "img"):
            # <a> a <img> preskocime (ale data dovnitr <a> zpracuje handle_data)
            return

        attrs_str = ""
        if attrs:
            attrs_str = " " + " ".join(
                f'{name}="{value}"' for name, value in attrs if value is not None
            )
        self._parts.append(f"<{tag_lower}{attrs_str}>")

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        tag_lower = tag.lower()
        if tag_lower in ("a", "img"):
            return
        self._parts.append(f"</{tag_lower}>")

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if data:
            self._parts.append(data)

    def get_html(self) -> str:
        return "".join(self._parts)


def clean_content_html(raw_html: str, source_url: Optional[str]) -> str:
    """
    Vrati HTML skoro stejne jako zdrojove,
    jen bez <a> a <img> tagu (text uvnitr <a> zustane).
    """
    html = raw_html or ""

    if source_url:
        # Smazat odkazy (<a>...</a>) odkazujici na zdrojovy URL (s/bez lomitka na konci).
        base_url = source_url.rstrip("/")
        pattern = re.compile(
            r'<a\b[^>]*href=["\']'
            + re.escape(base_url)
            + r'/??["\'][^>]*>.*?</a>',
            flags=re.IGNORECASE | re.DOTALL,
        )
        html = pattern.sub("", html)

    # Odstranit attachment blok s Files, at se textove neukazuje seznam priloh
    html = re.sub(
        r'<div[^>]+id=["\']wpba_attachment_list["\'][\s\S]*?</div>',
        "",
        html,
        flags=re.IGNORECASE,
    )
    # Odstranit i horizontalni caru s tridou wpba_attachment_hr
    html = re.sub(
        r'<hr\b[^>]*class=["\'][^"\']*\bwpba_attachment_hr\b[^"\']*["\'][^>]*>',
        "",
        html,
        flags=re.IGNORECASE,
    )

    parser = _StripLinksAndImagesParser()
    parser.feed(html)
    return parser.get_html().strip()


def load_articles(news_root: Path) -> List[Article]:
    if not news_root.exists():
        raise FileNotFoundError(f"Adresar neexistuje: {news_root}")

    folders = [p for p in news_root.iterdir() if p.is_dir()]
    # Zpracovavame odzadu: nejvyssi cislo slozky jako prvni (napr. 100 -> 1).
    folders.sort(
        key=lambda p: (
            not p.name.isdigit(),
            -(int(p.name)) if p.name.isdigit() else p.name,
        )
    )

    articles: List[Article] = []
    for folder in folders:
        article_json = folder / "article.json"
        if not article_json.exists():
            print(f"[SKIP] Chybi soubor: {article_json}")
            continue

        with article_json.open("r", encoding="utf-8") as f:
            data = json.load(f)

        title = (data.get("Title") or "").strip()
        annotation = (data.get("Annotation") or "").strip()
        raw_content_html = (data.get("Content") or "").strip()
        source_url = (data.get("Url") or "").strip()
        content_html = clean_content_html(raw_content_html, source_url or None)

        if not title:
            print(f"[SKIP] Prazdny Title: {article_json}")
            continue

        main_image_name = (data.get("MainImage") or "").strip()
        main_image: Optional[Path] = None
        if main_image_name:
            main_image_path = folder / main_image_name
            if not main_image_path.exists():
                print(f"[WARN] MainImage soubor nenalezen: {main_image_path}")
            else:
                main_image = main_image_path

        # Obrázky: standardně bereme všechny soubory ze složky "Images"
        content_images: List[Path] = []
        images_dir = folder / "Images"
        if images_dir.exists():
            for img_path in sorted(images_dir.iterdir()):
                if img_path.is_file():
                    content_images.append(img_path)
        else:
            # Fallback: pokud složka "Images" neexistuje, použijeme případné cesty z JSONu.
            for rel_path in data.get("ContentImages") or []:
                rel_path = (rel_path or "").strip()
                if not rel_path:
                    continue
                img_path = folder / rel_path
                if not img_path.exists():
                    print(f"[WARN] ContentImages soubor nenalezen: {img_path}")
                    continue
                content_images.append(img_path)

        # Soubory: standardně bereme všechny soubory ze složky "Files"
        files: List[Path] = []
        files_dir = folder / "Files"
        if files_dir.exists():
            for file_path in sorted(files_dir.iterdir()):
                if file_path.is_file():
                    files.append(file_path)
        else:
            # Fallback: pokud složka "Files" neexistuje, použijeme případné cesty z JSONu.
            for rel_path in data.get("Files") or []:
                rel_path = (rel_path or "").strip()
                if not rel_path:
                    continue
                file_path = folder / rel_path
                if not file_path.exists():
                    print(f"[WARN] Files soubor nenalezen: {file_path}")
                    continue
                files.append(file_path)

        articles.append(
            Article(
                folder=folder,
                title=title,
                annotation=annotation,
                content_html=content_html,
                main_image=main_image,
                content_images=content_images,
                files=files,
            )
        )

    return articles


def login(page: Page, email: str, password: str) -> None:
    page.goto(TENANT_URL, wait_until="domcontentloaded")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    page.locator("#Email").first.fill(email)
    page.locator("#Password").first.fill(password)
    try:
        page.locator("button:has-text('V pořádku')").first.click(timeout=3000)
    except TimeoutError:
        pass

    # Enter odesila login formular i kdyz click obcas blokuje cookie vrstva.
    page.locator("#Password").first.press("Enter")

    try:
        page.wait_for_url("**/admin**", timeout=15000)
    except TimeoutError:
        # Nektere redirekty po loginu nekonci pod /admin, proto staci pockat na load.
        page.wait_for_load_state("networkidle", timeout=15000)


def _set_value_by_selector(page: Page, selector: str, value: str) -> bool:
    """Nastavi hodnotu do input/textarea (vcetne hidden) a posle input/change event."""
    try:
        if page.locator(selector).count() == 0:
            return False
        page.evaluate(
            """([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }""",
            [selector, value],
        )
        return True
    except Exception:
        return False


def _set_content_for_active_language(page: Page, content_html: str) -> None:
    """Nastavi obsah aktualne aktivni jazykove verze ve WYSIWYG."""
    if not content_html:
        return

    filled_iframe = False
    for frame in page.frames:
        try:
            frame.wait_for_selector(
                ".iframe-wysiwyg-preview-body",
                timeout=2000,
            )
            frame.evaluate(
                """(html) => {
                    const el = document.querySelector('.iframe-wysiwyg-preview-body');
                    if (el) {
                        el.innerHTML = html;
                    }
                }""",
                content_html,
            )
            filled_iframe = True
            break
        except TimeoutError:
            continue
        except Exception as exc:  # noqa: BLE001
            print(
                "[WARN] Chyba pri nastavovani WYSIWYG obsahu "
                f"v iframe-wysiwyg-preview-body: {exc}"
            )
            break
    if not filled_iframe:
        print(
            "[WARN] Nepodarilo se najit iframe s tridou "
            "'iframe-wysiwyg-preview-body' pro nastaveni obsahu."
        )

    # Zapis i do content poli formulare, ale ne do prekladovych poli.
    try:
        page.evaluate(
            """(html) => {
                const candidates = Array.from(
                  document.querySelectorAll('textarea,input[type="hidden"]')
                ).filter(el => {
                  const id = (el.id || '').toLowerCase();
                  const name = (el.name || '').toLowerCase();
                  const hasContent = id.includes('content') || name.includes('content');
                  const isTranslation = id.includes('translations') || name.includes('translations');
                  return hasContent && !isTranslation;
                });
                for (const el of candidates) {
                  el.value = html;
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""",
            content_html,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            "[WARN] Chyba pri nastavovani skryteho pole pro content: "
            f"{exc}"
        )


def _upload_media_for_article(page: Page, article: Article) -> None:
    """Nahraje MainImage, Images a Files pro aktualne aktivni jazyk."""
    # Nahrani hlavniho obrazku (MainImage) do <input type="file"
    # class="js-file-upload form-control" accept="image/*"> (prip. obdobneho).
    if article.main_image:
        print(f"[INFO] Nahravam MainImage: {article.main_image}")
        try:
            # Pouzijeme primo file input podle tridy a accept atributu.
            main_image_locator = page.locator(
                "input.js-file-upload.form-control[accept*='image']"
            ).first
            main_image_locator.set_input_files(str(article.main_image))

            # Pockat, nez se po uploadu vyplni hidden pole s cestou k obrazku,
            # pokud existuje (typicky Dto_Image_Path/js-file-path).
            try:
                page.wait_for_function(
                    """() => {
                        const el = document.querySelector('#Dto_Image_Path');
                        return !!(el && el.value);
                    }""",
                    timeout=30000,
                )
            except TimeoutError:
                print(
                    "[WARN] Dto_Image_Path se po uploadu MainImage nevyplnil "
                    "do 30s – obsah se ale mohl ulozit jinym mechanizmem."
                )
        except Exception as exc:  # noqa: BLE001
            print(
                "[WARN] Nepodarilo se nahrat MainImage do "
                "'input.js-file-upload.form-control[accept*=\"image\"]': "
                f"{exc}"
            )

    # Nahrani ostatnich obrazku (Images) pres <input type="file" class="js-images-upload">
    if article.content_images:
        # Prepnuti na zalozku / panel s obrazky, aby byl videt uploader.
        try:
            page.locator("a[href='#tab-Images'], #tab-Images, [data-bs-target='#collapseImages']").first.click()
            page.wait_for_timeout(500)
            # Pockat, nez se v tele tab-Images objevi input pro upload obrazku.
            page.wait_for_selector(
                "#tab-Images-body input.js-images-upload",
                timeout=5000,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "[WARN] Nepodarilo se prepnout na zalozku Images (tab-Images) "
                f"nebo najit input: {exc}"
            )

        image_paths = [str(p) for p in article.content_images]
        print(f"[INFO] Nahravam Images: {image_paths}")
        try:
            # Primarne hledame input primo v tab-Images.
            images_locator = page.locator(
                "#tab-Images-body input.js-images-upload"
            ).first
            if images_locator.count() == 0:
                # Fallback: prvni input s tridou js-images-upload kdekoliv.
                images_locator = page.locator("input.js-images-upload").first

            # Input ma atribut multiple, muzeme nahrat vsechny obrazky najednou.
            images_locator.set_input_files(image_paths)
            page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001
            print(
                "[WARN] Nepodarilo se nahrat Images pres "
                f"'.js-images-upload': {exc}"
            )

    # Nahrani souboru (Files) do <input type="file" class="js-files-upload form-control">
    if article.files:
        # Nejprve prepnout na zalozku se soubory pres id="tab-Files",
        # aby byl videt uploader.
        try:
            page.locator("a[href='#tab-Files'], #tab-Files, [data-bs-target='#collapseFiles']").first.click()
            page.wait_for_timeout(500)
            # Pockat, nez se na zalozce objevi input pro upload souboru.
            page.wait_for_selector(
                "#tab-Files-body input.js-files-upload",
                timeout=5000,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "[WARN] Nepodarilo se prepnout na zalozku Files (tab-Files) "
                f"nebo najit input: {exc}"
            )
            # Fallback: puvodni klik na rozbalovaci blok, pokud existuje.
            try:
                page.locator(
                    ".collapse--section-block.js-tab-content.collapse.show"
                ).first.click()
            except Exception as exc2:  # noqa: BLE001
                print(
                    "[WARN] Nepodarilo se ani kliknout na sekci se soubory "
                    "(collapse--section-block js-tab-content collapse show): "
                    f"{exc2}"
                )

        file_paths = [str(p) for p in article.files]
        print(f"[INFO] Nahravam Files: {file_paths}")

        try:
            # Primarne hledame input primo v tab-Files.
            files_locator = page.locator(
                "#tab-Files-body input.js-files-upload"
            ).first
            if files_locator.count() == 0:
                # Fallback: prvni input s tridou js-files-upload form-control kdekoliv.
                files_locator = page.locator(
                    "input.js-files-upload"
                ).first

            # Input ma atribut multiple, muzeme nahrat vsechny soubory najednou.
            files_locator.set_input_files(file_paths)
            page.wait_for_timeout(500)

            # U souboru vypneme vykreslovani PDF nahledu (IsPreviewRendered).
            for _ in range(5):
                unchecked = page.evaluate(
                    """() => {
                        const checkboxes = Array.from(
                          document.querySelectorAll(
                            'input[type="checkbox"].form-check-input'
                          )
                        ).filter(el => {
                          const id = el.id || '';
                          const name = el.name || '';
                          return id.includes('_IsPreviewRendered')
                            || name.endsWith('.IsPreviewRendered')
                            || name.includes('.IsPreviewRendered');
                        });

                        let changed = 0;
                        for (const cb of checkboxes) {
                          if (cb.checked) {
                            cb.checked = false;
                            cb.dispatchEvent(new Event('input', { bubbles: true }));
                            cb.dispatchEvent(new Event('change', { bubbles: true }));
                            changed += 1;
                          }
                        }
                        return { total: checkboxes.length, changed };
                    }"""
                )
                if unchecked.get("total", 0) > 0:
                    print(
                        "[INFO] IsPreviewRendered checkboxu nalezeno: "
                        f"{unchecked.get('total', 0)}, odskrtnuto: {unchecked.get('changed', 0)}"
                    )
                    break
                page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001
            print(
                "[WARN] Nepodarilo se nahrat soubory do "
                "'input.js-files-upload.form-control': "
                f"{exc}"
            )


def _add_and_fill_en_translation(page: Page, en_article: Article) -> None:
    """Prida EN preklad a vyplni title/annotation/content z NewsEN."""
    try:
        page.locator(".btn.btn-link.btn-sm.p-0.js-language-add").first.click(timeout=5000)
        page.locator(
            ".btn.btn-link.js-language-add-confirm.js-language-add-confirm-en"
        ).first.click(timeout=5000)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Nepodarilo se pridat EN jazyk: {exc}")
        return

    try:
        # Aktivace EN jazyka pro editaci prekladu.
        activate = page.locator(
            ".btn.btn--language.js-language-activate:has-text('EN'), "
            ".btn.btn--language.js-language-activate[data-language='en'], "
            ".btn.btn--language.js-language-activate"
        ).first
        activate.click(timeout=5000)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Nepodarilo se aktivovat EN jazyk: {exc}")
        return

    page.wait_for_timeout(500)

    # Vyplnujeme EN prekladova pole robustne i kdyz jsou docasne skryta.
    if not _set_value_by_selector(page, "#Dto_Translations_en__Title", en_article.title):
        if not _set_value_by_selector(
            page,
            ".js-ai-admin-option.js-auto-fill-title.title-en.form-control.js-input",
            en_article.title,
        ):
            print("[WARN] Nepodarilo se vyplnit EN title (Dto_Translations_en__Title/title-en).")

    if not _set_value_by_selector(page, "#Dto_Translations_en__Annotation", en_article.annotation):
        if not _set_value_by_selector(
            page,
            ".js-ai-admin-option.annotation-en.form-control.js-input",
            en_article.annotation,
        ):
            print(
                "[WARN] Nepodarilo se vyplnit EN annotation "
                "(Dto_Translations_en__Annotation/annotation-en)."
            )

    if en_article.content_html:
        if not _set_value_by_selector(
            page, "#Dto_Translations_en__Content", en_article.content_html
        ):
            print("[WARN] Nepodarilo se vyplnit #Dto_Translations_en__Content pro EN preklad.")

        # Pro jistotu propiseme i do aktivniho WYSIWYG iframe po aktivaci jazyka.
        _set_content_for_active_language(page, en_article.content_html)


def create_news_item(page: Page, article: Article, en_article: Optional[Article]) -> None:
    page.goto(NEWS_INDEX_URL, wait_until="domcontentloaded")
    page.locator("a[href='/Core/NewsAdmin/News/Edit']").first.click()

    page.wait_for_selector("#Dto_Name", timeout=10000)

    # Nejdriv vyplnime puvodni data z News.
    page.fill("#Dto_Name", article.title)
    page.fill("#Dto_Translations_it__Title", article.title)
    page.fill("#Dto_Translations_it__Annotation", article.annotation)

    # Naplneni WYSIWYG obsahu z Article.content_html
    if article.content_html:
        # V zakladnim jazyce plnime obsah z News.
        if not _set_value_by_selector(
            page, "#Dto_Translations_it__Content", article.content_html
        ):
            print("[WARN] Nepodarilo se vyplnit #Dto_Translations_it__Content pro puvodni text.")
        _set_content_for_active_language(page, article.content_html)

    # Zakliknuti pozadovaneho checkboxu
    try:
        page.check("#form-check-input-8497")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Nepodarilo se zaskrtnout checkbox #form-check-input-8497: {exc}")

    # Nahrajeme media pro puvodni jazyk ze slozky News.
    _upload_media_for_article(page, article)

    # Po vyplneni puvodnich dat pridame EN preklad z odpovidajici slozky NewsEN.
    # V prekladu neuploadujeme Files/Images.
    if en_article is not None:
        _add_and_fill_en_translation(page, en_article)
    else:
        print(
            f"[WARN] Chybi EN clanek pro slozku {article.folder.name} "
            "(NewsEN), preklad nebude vyplnen."
        )

    page.locator(".btn.btn-primary.js-onetime-submit").first.click()
    page.wait_for_url("**/Core/NewsAdmin/News/Index**", timeout=15000)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Naplni CMS novinky z adresare News/*/article.json"
    )
    parser.add_argument(
        "--news-root",
        type=Path,
        default=Path("News"),
        help="Cesta k root adresari s podslozkami novinek (default: News)",
    )
    parser.add_argument(
        "--news-en-root",
        type=Path,
        default=Path("NewsEN"),
        help="Cesta k root adresari EN novinek (default: NewsEN)",
    )
    parser.add_argument(
        "--only-folders",
        nargs="+",
        help=(
            "Zpracuje jen slozky s danymi nazvy (napr. --only-folders 1 5 10). "
            "Filtruje podle nazvu podslozky v adresari News."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Kolik clanku maximalne zpracovat (0 = vsechny). Pro test dejte --limit 1.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Spusti browser v headless rezimu (default je viditelny browser).",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("CMS_EMAIL", DEFAULT_EMAIL),
        help="Login email (default z CMS_EMAIL nebo predvyplnena hodnota).",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("CMS_PASSWORD", DEFAULT_PASSWORD),
        help="Login heslo (default z CMS_PASSWORD nebo predvyplnena hodnota).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Jen vypise, co by se nahravalo, bez otevreni browseru.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    articles = load_articles(args.news_root)
    en_articles = load_articles(args.news_en_root)
    en_by_folder_name = {a.folder.name: a for a in en_articles}

    if args.only_folders:
        wanted = {str(name) for name in args.only_folders}
        articles = [a for a in articles if a.folder.name in wanted]

    if args.limit and args.limit > 0:
        articles = articles[: args.limit]

    if not articles:
        print("Nebyly nalezeny zadne clanky ke zpracovani.")
        return

    print(f"Nalezeno clanku ke zpracovani: {len(articles)}")
    for idx, article in enumerate(articles, start=1):
        print(f"  {idx}. {article.folder.name} -> {article.title}")

    if args.dry_run:
        print("Dry run hotovo.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context()
        page = context.new_page()

        print("Prihlasuji do CMS...")
        login(page, args.email, args.password)
        print("Prihlaseni dokonceno.")

        for idx, article in enumerate(articles, start=1):
            print(f"[{idx}/{len(articles)}] Vytvarim novinku z: {article.folder}")
            en_article = en_by_folder_name.get(article.folder.name)
            create_news_item(page, article, en_article)

        context.close()
        browser.close()

    print("Hotovo.")


if __name__ == "__main__":
    main()
