"""
╔══════════════════════════════════════════════╗
║         MangaFire.to — Manga Downloader      ║
║  Uses DrissionPage to bypass Cloudflare      ║
╚══════════════════════════════════════════════╝

Usage:
    python3 main.py

Requirements (auto-installed):
    pip install DrissionPage requests
"""

import os
import sys
import re
import time
import json
import requests
from pathlib import Path


# ─── Dependency check ─────────────────────────────────────────────────────────

def install_if_missing(package, import_name=None):
    try:
        __import__(import_name or package)
    except ImportError:
        print(f"  📦 Installing {package}...")
        ret = os.system(f'{sys.executable} -m pip install {package} --break-system-packages -q')
        if ret != 0:
            os.system(f'{sys.executable} -m pip install {package} -q')

print("\n🔧 Checking dependencies...")
for pkg, imp in [
    ("DrissionPage", "DrissionPage"),
    ("requests",     "requests"),
]:
    install_if_missing(pkg, imp)
print("  ✅ All dependencies ready.\n")

from DrissionPage import ChromiumPage, ChromiumOptions


# ─── Config ───────────────────────────────────────────────────────────────────

def _detect_chromium() -> tuple[str, str]:
    """
    Find the Chromium binary and return (binary_path, ua_version).
    Returns ("", "135.0.0.0") if nothing is found (version is a safe fallback).
    """
    import subprocess
    import shutil

    candidates = [
        # Linux
        "chromium-browser",
        "chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
        # macOS
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        # Windows
        r"C:\Program Files\Chromium\Application\chrome.exe",
        r"C:\Program Files (x86)\Chromium\Application\chrome.exe",
    ]

    for cand in candidates:
        resolved = shutil.which(cand) or (cand if os.path.isfile(cand) else None)
        if not resolved:
            continue
        try:
            out = subprocess.check_output(
                [resolved, "--version"], stderr=subprocess.DEVNULL, timeout=5
            ).decode().strip()
            # Output is typically: "Chromium 124.0.6367.207 snap"
            m = re.search(r'[\d]+\.[\d.]+', out)
            version = m.group(0) if m else "135.0.0.0"
            major = version.split(".")[0]
            ua_version = f"{major}.0.0.0"
            print(f"  🔍 Detected Chromium {version} → UA version {ua_version}")
            return resolved, ua_version
        except Exception:
            continue

    print("  ⚠️  Could not detect Chromium version — using fallback UA version.")
    return "", "135.0.0.0"


_CHROMIUM_BIN, _CHROMIUM_UA_VERSION = _detect_chromium()

DOWNLOAD_HEADERS = {
    "User-Agent": (
        f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{_CHROMIUM_UA_VERSION} Safari/537.36"
    ),
    "Referer": "https://mangafire.to/",
}

BETWEEN_CHAPTERS   = 4
IMAGE_TIMEOUT      = 20
CLOUDFLARE_WAIT    = 10   # seconds to let Cloudflare JS challenge complete
PAGE_AJAX_WAIT     = 8    # seconds to let AJAX finish after page load


# ─── Helpers ──────────────────────────────────────────────────────────────────

def fmt_num(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


def parse_chapter_number(url: str) -> float:
    m = re.search(r'/chapter-([\d.]+)', url)
    if m:
        return float(m.group(1))
    raise ValueError(f"No chapter number in: {url}")


def chapter_url(base_url: str, n: float) -> str:
    return re.sub(r'/chapter-[\d.]+$', f'/chapter-{fmt_num(n)}', base_url)


def chapter_folder_name(n: float) -> str:
    if n == int(n):
        return f"Chapter {int(n):03d}"
    whole = int(n)
    dec   = round((n - whole) * 10)
    return f"Chapter {whole:03d}.{dec}"


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def download_image(url: str, dest: Path, session: requests.Session) -> bool:
    for attempt in range(3):
        try:
            r = session.get(url, headers=DOWNLOAD_HEADERS, timeout=IMAGE_TIMEOUT, stream=True)
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            return True
        except Exception as e:
            if attempt == 2:
                print(f"\n      ⚠️  Failed: {e}")
            else:
                time.sleep(2)
    return False


# ─── Browser setup ────────────────────────────────────────────────────────────

def create_browser() -> ChromiumPage:
    """
    Create a DrissionPage ChromiumPage instance.
    DrissionPage drives a REAL Chromium window (not headless) which
    passes Cloudflare's JS/TLS fingerprint checks.
    The Chromium binary and its version are auto-detected at startup.
    """
    opts = ChromiumOptions()

    # Use auto-detected Chromium binary (resolved at import time by _detect_chromium)
    if _CHROMIUM_BIN:
        opts.set_browser_path(_CHROMIUM_BIN)
        print(f"  🌐 Using Chromium binary: {_CHROMIUM_BIN}")
    else:
        print("  ⚠️  Chromium not found in common paths — falling back to default browser.")

    # Run VISIBLE — Cloudflare detects headless, a real window passes every time
    # The window will appear on your screen briefly while downloading
    opts.headless(False)

    opts.set_argument("--no-sandbox")
    opts.set_argument("--disable-dev-shm-usage")
    opts.set_argument("--mute-audio")
    opts.set_argument("--window-size=1280,800")

    # Images MUST be enabled — MangaFire uses IntersectionObserver for lazy loading.
    # If images are blocked the observer never fires and only the first ~4 pages load.

    page = ChromiumPage(opts)
    page.set.timeouts(page_load=40, script=20)

    # Inject XHR/fetch hook before any page script runs on every navigation.
    # This way the very first AJAX call (which carries all image URLs) is captured.
    # DrissionPage run_cdp uses keyword args, not a dict as 2nd positional
    HOOK_SCRIPT = "window.__mf_hooked=true; window.__mf_log=[];const _ft=window.fetch; window.fetch=function(){const u=(arguments[0]&&arguments[0].url)?arguments[0].url:String(arguments[0]);return _ft.apply(this,arguments).then(function(r){r.clone().text().then(function(t){window.__mf_log.push({url:u,body:t});}).catch(function(){});return r;});};const _op=XMLHttpRequest.prototype.open,_sd=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.open=function(m,u){this.__u=u;return _op.apply(this,arguments);};XMLHttpRequest.prototype.send=function(){this.addEventListener('load',function(){try{window.__mf_log.push({url:this.__u,body:this.responseText});}catch(e){}});return _sd.apply(this,arguments);};"
    try:
        page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=HOOK_SCRIPT)
    except Exception:
        # Older DrissionPage versions use different signature
        try:
            page.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_SCRIPT})
        except Exception:
            pass  # Will fall back to inject_hooks() per-page

    return page


def wait_for_cloudflare(page: ChromiumPage):
    """Wait until Cloudflare challenge is gone (URL is not the challenge page)."""
    for _ in range(30):
        url = page.url or ""
        title = page.title or ""
        if "just a moment" in title.lower() or "cloudflare" in title.lower():
            time.sleep(1)
        else:
            time.sleep(1)
            return
    print("  ⚠️  Cloudflare may still be active — continuing anyway")


# ─── Network request logging via CDP ─────────────────────────────────────────

def enable_network_logging(page: ChromiumPage):
    """Enable CDP Network domain so we can capture response bodies."""
    try:
        page.run_cdp("Network.enable")
    except Exception:
        pass


# ─── Get all AJAX responses from current page ─────────────────────────────────

INTERCEPT_JS = """
// Inject once — wraps fetch() and XHR to log all responses
if (!window.__mf_hooked) {
    window.__mf_hooked = true;
    window.__mf_log = [];

    // Hook fetch
    const _fetch = window.fetch;
    window.fetch = function() {
        const url = (arguments[0] && arguments[0].url) ? arguments[0].url : String(arguments[0]);
        return _fetch.apply(this, arguments).then(function(resp) {
            resp.clone().text().then(function(t) {
                window.__mf_log.push({url: url, body: t});
            }).catch(function(){});
            return resp;
        });
    };

    // Hook XHR
    const _open = XMLHttpRequest.prototype.open;
    const _send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(m, u) {
        this.__u = u;
        return _open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
        this.addEventListener('load', function() {
            try { window.__mf_log.push({url: this.__u, body: this.responseText}); } catch(e) {}
        });
        return _send.apply(this, arguments);
    };
}
return 'hooked';
"""


def inject_hooks(page: ChromiumPage):
    try:
        page.run_js(INTERCEPT_JS)
    except Exception as e:
        pass


def get_log(page: ChromiumPage) -> list:
    try:
        return page.run_js("return window.__mf_log || [];") or []
    except Exception:
        return []


def clear_log(page: ChromiumPage):
    try:
        page.run_js("window.__mf_log = [];")
    except Exception:
        pass


# ─── Extract data from JSON ───────────────────────────────────────────────────

def walk_json_for_images(data, depth=0) -> list:
    if depth > 12:
        return []
    results = []
    if isinstance(data, str):
        if data.startswith("http") and re.search(r'\.(jpe?g|png|webp)', data, re.I):
            results.append(data)
    elif isinstance(data, list):
        for x in data:
            results.extend(walk_json_for_images(x, depth + 1))
    elif isinstance(data, dict):
        for v in data.values():
            results.extend(walk_json_for_images(v, depth + 1))
    return results


def walk_json_for_chapters(data, depth=0) -> set:
    if depth > 12:
        return set()
    found = set()
    if isinstance(data, dict):
        for k, v in data.items():
            if k in ("number", "chapter", "num", "chapterNumber", "chapter_number"):
                try:
                    found.add(float(v))
                except Exception:
                    pass
            found.update(walk_json_for_chapters(v, depth + 1))
    elif isinstance(data, list):
        for x in data:
            found.update(walk_json_for_chapters(x, depth + 1))
    return found


def parse_log_for_images(log: list) -> list:
    images = []
    for entry in log:
        body = entry.get("body", "")
        if not body:
            continue
        # Try JSON parse first
        try:
            data = json.loads(body)
            images.extend(walk_json_for_images(data))
        except Exception:
            pass
        # Raw URL scan regardless
        for m in re.finditer(r'https?://\S+?\.(?:jpe?g|png|webp)(?:\?\S*)?', body, re.I):
            images.append(m.group(0).strip('"').strip("'").strip("\\"))
    # Deduplicate
    seen, out = set(), []
    for img in images:
        if img not in seen:
            seen.add(img)
            out.append(img)
    return out


def parse_log_for_chapters(log: list) -> set:
    chapters = set()
    for entry in log:
        body = entry.get("body", "")
        url  = entry.get("url", "")
        if not body:
            continue
        try:
            data = json.loads(body)
            chapters.update(walk_json_for_chapters(data))
        except Exception:
            pass
        for m in re.finditer(r'/chapter-([\d.]+)', body + url):
            try:
                chapters.add(float(m.group(1)))
            except Exception:
                pass
        for m in re.finditer(r'"number"\s*:\s*["\']?([\d.]+)', body):
            try:
                chapters.add(float(m.group(1)))
            except Exception:
                pass
    return chapters


# ─── Get chapter list ─────────────────────────────────────────────────────────

def get_chapter_list(page: ChromiumPage, slug: str) -> list:
    info_url = f"https://mangafire.to/manga/{slug}"
    print(f"  📖 Navigating to: {info_url}")

    page.get(info_url)
    wait_for_cloudflare(page)
    inject_hooks(page)

    # Give AJAX calls time to fire
    time.sleep(PAGE_AJAX_WAIT)

    # Scroll to trigger lazy-loaded chapter list
    try:
        page.run_js("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        page.run_js("window.scrollTo(0, 0);")
        time.sleep(1)
    except Exception:
        pass

    log      = get_log(page)
    chapters = parse_log_for_chapters(log)

    print(f"  🔎 Intercepted {len(log)} network calls, found {len(chapters)} chapter numbers in responses")

    # Also scrape DOM links
    try:
        for el in page.eles("tag:a"):
            href = el.attr("href") or ""
            m = re.search(r'/chapter-([\d.]+)', href)
            if m:
                chapters.add(float(m.group(1)))
    except Exception:
        pass

    # Scrape page source too
    try:
        src = page.html or ""
        for m in re.finditer(r'/chapter-([\d.]+)', src):
            chapters.add(float(m.group(1)))
    except Exception:
        pass

    # Remove chapter 0 if we have a real list (it's sometimes a fake)
    if len(chapters) > 5 and 0.0 in chapters:
        chapters.discard(0.0)

    return sorted(chapters)


# ─── Get chapter images ───────────────────────────────────────────────────────

def get_chapter_images(page: ChromiumPage, url: str) -> list:
    # The CDP hook installed in create_browser() runs before any page script,
    # so it catches the very first AJAX call that carries ALL image URLs.
    # We just need to navigate and wait — no scrolling required for the data.
    clear_log(page)
    page.get(url)
    wait_for_cloudflare(page)

    # Wait for the reader's initial AJAX call to complete
    print(f"  ⏳ Waiting for image data to load...", end="", flush=True)
    time.sleep(PAGE_AJAX_WAIT)

    # ── Check if we already have all images from the AJAX log ─────────────────
    images = parse_log_for_images(get_log(page))
    print(f"\r  📡 AJAX log: {len(images)} image URLs captured.          ")

    # ── Fallback: use scrollIntoView on every <img> to force lazy-load ────────
    # If the AJAX log didn't give us everything, call scrollIntoView on each img
    # element — this is the most reliable way to trigger IntersectionObserver
    # without fighting which container is scrollable.
    if len(images) < 5:
        print(f"  📜 Triggering lazy-load via scrollIntoView on all img elements...")
        try:
            # First dismiss the header/menu with keypresses (H and M)
            try:
                page.actions.key("h")
                time.sleep(0.3)
                page.actions.key("m")
                time.sleep(0.3)
            except Exception:
                pass

            # Get total img count so we know how many to iterate
            img_count = page.run_js("return document.querySelectorAll('img').length;") or 0
            print(f"  🖼  Found {img_count} <img> elements in DOM. Scrolling each into view...")

            last_log_size = len(get_log(page))
            for i in range(img_count):
                page.run_js(f"""
                    var imgs = document.querySelectorAll('img');
                    if (imgs[{i}]) {{
                        imgs[{i}].scrollIntoView({{behavior:'instant', block:'center'}});
                    }}
                """)
                time.sleep(0.4)

                # Every 5 images check if the log grew
                if i % 5 == 0:
                    new_size = len(get_log(page))
                    new_imgs = len(parse_log_for_images(get_log(page)))
                    if new_size > last_log_size:
                        last_log_size = new_size
                        sys.stdout.write(f"\r  📜 scrollIntoView progress: {i+1}/{img_count} — {new_imgs} pages found   ")
                        sys.stdout.flush()

            time.sleep(2)
            print()
            images = parse_log_for_images(get_log(page))
            print(f"  📡 After scrollIntoView: {len(images)} image URLs total.")

        except Exception as e:
            print(f"\n  ⚠️  scrollIntoView error: {e}")

    # ── Final DOM fallback: read src/data-src off every visible <img> ─────────
    if len(images) < 5:
        print(f"  🔍 DOM fallback: reading img src attributes directly...")
        try:
            dom_imgs = page.run_js("""
                return Array.from(document.querySelectorAll('img')).map(function(el) {
                    return {
                        src: el.src || el.getAttribute('data-src') || el.getAttribute('data-original') || '',
                        w:   el.naturalWidth  || el.width  || 0,
                        h:   el.naturalHeight || el.height || 0
                    };
                }).filter(function(i) { return i.src && i.w > 200 && i.h > 200; });
            """) or []
            for img in dom_imgs:
                src = img.get("src", "")
                if src and re.search(r'\.(jpe?g|png|webp)', src, re.I):
                    images.append(src)
        except Exception:
            pass

    # ── Deduplicate and filter out non-manga URLs ──────────────────────────────
    EXCLUDE = [r'/logo', r'/icon', r'favicon', r'/avatar', r'\.svg',
               r'data:image', r'mangafire\.to/assets']
    seen, clean = set(), []
    for img in images:
        img = img.strip()
        if img and img not in seen and not any(re.search(p, img, re.I) for p in EXCLUDE):
            seen.add(img)
            clean.append(img)

    return clean


# ─── Download a chapter ───────────────────────────────────────────────────────

def download_chapter(ch_num, url, manga_root, session, page) -> int:
    folder   = manga_root / chapter_folder_name(ch_num)
    existing = list(folder.glob("*.*"))
    if len(existing) >= 5:
        print(f"  ⏭️  Chapter {fmt_num(ch_num)} already saved ({len(existing)} pages). Skipping.")
        return len(existing)

    print(f"  ⏳ Loading chapter {fmt_num(ch_num)}...", end="", flush=True)
    images = get_chapter_images(page, url)
    print(f"\r  📄 Found {len(images)} pages.           ")

    if not images:
        return 0

    folder.mkdir(parents=True, exist_ok=True)
    success = 0
    for i, img_url in enumerate(images, 1):
        ext_m = re.search(r'\.(jpe?g|png|webp)', img_url, re.I)
        ext   = ext_m.group(0).lower() if ext_m else ".jpg"
        if ext == ".jpeg":
            ext = ".jpg"
        dest = folder / f"page_{i:03d}{ext}"
        if download_image(img_url, dest, session):
            success += 1
        sys.stdout.write(f"\r  💾 {success}/{len(images)} pages saved...   ")
        sys.stdout.flush()

    status = "✅" if success == len(images) else "⚠️ "
    print(f"\r  {status} Chapter {fmt_num(ch_num)} — {success}/{len(images)} pages.            ")
    return success


# ─── Prompt ───────────────────────────────────────────────────────────────────

def prompt_single_manga(index: int, default_location: str) -> dict:
    """Prompt the user for one manga's details."""
    print(f"\n── Manga {index} " + "─" * 40)

    while True:
        url = input("Set link (chapter number auto-increments):\n  → ").strip()
        if re.search(r'mangafire\.to/read/.+/chapter-[\d.]+', url):
            break
        print("  ⚠️  Must be a MangaFire reader URL, e.g.:\n"
              "  https://mangafire.to/read/grand-bluee.lxx3/en/chapter-0\n")

    raw_name    = input("\nFolder name (e.g. Grand Blue Dreaming):\n  → ").strip()
    folder_name = sanitize(raw_name) or f"Manga_{index}"

    print(f"\nLocation (blank = [{default_location}]):")
    location  = input("  → ").strip() or default_location
    save_path = Path(location) / folder_name

    return {"url": url, "folder_name": folder_name, "save_path": save_path}


# ─── Chapter selection ────────────────────────────────────────────────────────

def parse_chapter_selection(raw: str, available: list) -> list:
    """
    Parse a user selection string into a sorted list of chapter numbers.

    Accepted formats (comma-separated, mix-and-match):
      all          → every chapter
      1-50         → chapters 1 through 50 (inclusive, by chapter number)
      9            → single chapter 9
      151.5        → decimal chapter
      9,25,46      → explicit list
      1-50,75,100  → range + singles
    """
    raw = raw.strip()
    if raw.lower() == "all":
        return list(available)

    avail_set = {ch: ch for ch in available}   # exact match by float
    selected  = set()

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        # Range  e.g. 1-50
        range_m = re.match(r'^([\d.]+)\s*-\s*([\d.]+)$', part)
        if range_m:
            lo, hi = float(range_m.group(1)), float(range_m.group(2))
            for ch in available:
                if lo <= ch <= hi:
                    selected.add(ch)
        else:
            # Single value
            try:
                val = float(part)
                if val in avail_set:
                    selected.add(val)
                else:
                    print(f"  ⚠️  Chapter {fmt_num(val)} not in list — skipping.")
            except ValueError:
                print(f"  ⚠️  Could not parse '{part}' — skipping.")

    return sorted(selected)


def prompt_chapter_selection(chapters: list) -> list:
    """
    Show available chapters and let the user choose which ones to download.
    Returns the selected subset (sorted).
    """
    print(f"\n  📑 Available chapters ({len(chapters)} total):")

    # Print in rows of 10 for readability
    row = []
    for ch in chapters:
        row.append(fmt_num(ch))
        if len(row) == 10:
            print("    " + "  ".join(row))
            row = []
    if row:
        print("    " + "  ".join(row))

    print("\n  Select chapters to download:")
    print("    all          → download everything")
    print("    1-50         → chapters 1 through 50")
    print("    9,25,46      → specific chapters")
    print("    1-50,75,100  → mix of range and singles")

    while True:
        raw = input("\n  → ").strip()
        if not raw:
            print("  ⚠️  Please enter a selection.")
            continue
        result = parse_chapter_selection(raw, chapters)
        if not result:
            print("  ⚠️  No valid chapters selected — try again.")
            continue
        print(f"\n  ✅ Selected {len(result)} chapter(s): "
              f"{fmt_num(result[0])} → {fmt_num(result[-1])}")
        return result


# ─── Download one manga (browser already open) ────────────────────────────────

def download_manga(cfg: dict, page: ChromiumPage, session: requests.Session,
                   manga_index: int, manga_total):
    url       = cfg["url"]
    save_path = cfg["save_path"]

    m    = re.search(r'mangafire\.to/read/([^/]+)/([^/]+)/chapter', url)
    slug = m.group(1) if m else ""

    save_path.mkdir(parents=True, exist_ok=True)

    print("\n📋 Discovering chapters...")
    chapters = get_chapter_list(page, slug)

    if not chapters:
        start    = parse_chapter_number(url)
        chapters = [start]
        print(f"  ⚠️  Could not get chapter list. Trying from chapter {fmt_num(start)}.")
    else:
        print(f"  ✅ Found {len(chapters)} chapters: "
              f"{fmt_num(min(chapters))} → {fmt_num(max(chapters))}")
        chapters = prompt_chapter_selection(chapters)

    total = len(chapters)
    print(f"\n📚 Downloading {total} selected chapter(s) — this may take a while...\n")

    downloaded, failed = 0, []

    for i, ch_num in enumerate(chapters, 1):
        ch_url = chapter_url(url, ch_num)
        print(f"[{i}/{total}] Chapter {fmt_num(ch_num)}")
        try:
            pages = download_chapter(ch_num, ch_url, save_path, session, page)
            if pages > 0:
                downloaded += 1
            else:
                failed.append(ch_num)
        except Exception as e:
            print(f"  ❌ Error: {e}")
            failed.append(ch_num)
        time.sleep(BETWEEN_CHAPTERS)

    last = chapters[-1] if chapters else "?"
    print("\n" + "─" * 54)
    print(f"✅ [{manga_index}/{manga_total}] Finished: {cfg['folder_name']}")
    print(f"   Saved {downloaded}/{total} chapters")
    print(f"   ({cfg['folder_name']} has {fmt_num(last)} chapters)")
    print(f"   📁 Location: {save_path}")
    if failed:
        print(f"   ⚠️  Failed chapters: {', '.join(fmt_num(c) for c in failed)}")
    print("─" * 54)

    return downloaded, total, failed


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("╔══════════════════════════════════════════════╗")
    print("║       MangaFire.to — Manga Downloader        ║")
    print("╚══════════════════════════════════════════════╝\n")

    session = requests.Session()
    session.headers.update(DOWNLOAD_HEADERS)

    print("🌐 Opening Chromium (visible window — needed to pass Cloudflare)...")
    print("   The browser window will open on your screen. Don't close it!\n")
    page = create_browser()

    overall_results = []
    default_location = os.getcwd()
    manga_index = 0

    try:
        while True:
            manga_index += 1
            cfg = prompt_single_manga(manga_index, default_location)

            # After the first manga, use its parent dir as the new default
            if manga_index == 1:
                default_location = str(cfg["save_path"].parent)

            print(f"\n{'═' * 54}")
            print(f"📚 Starting: {cfg['folder_name']}")
            print(f"📁 Save path: {cfg['save_path']}")
            print("═" * 54)

            downloaded, total, failed = download_manga(
                cfg, page, session, manga_index, "?"
            )
            overall_results.append({
                "name":       cfg["folder_name"],
                "downloaded": downloaded,
                "total":      total,
                "failed":     failed,
                "path":       cfg["save_path"],
            })

            print("\nDownload another manga? (y/n)")
            ans = input("  → ").strip().lower()
            if ans not in ("y", "yes"):
                break

    finally:
        page.quit()

    # ── Final summary ─────────────────────────────────────────────────────────
    print("\n" + "═" * 54)
    print(f"🏁 All done! Downloaded {len(overall_results)} manga(s):\n")
    for r in overall_results:
        status = "✅" if not r["failed"] else "⚠️ "
        print(f"  {status} {r['name']}")
        print(f"     Chapters: {r['downloaded']}/{r['total']}")
        print(f"     📁 {r['path']}")
        if r["failed"]:
            print(f"     ❌ Failed: {', '.join(fmt_num(c) for c in r['failed'])}")
    print("═" * 54 + "\n")


if __name__ == "__main__":
    main()