"""
╔══════════════════════════════════════════════╗
║         MangaFire.to — Manga Downloader      ║
║  Uses DrissionPage to bypass Cloudflare      ║
╚══════════════════════════════════════════════╝

Requirements (auto-installed):
    pip install DrissionPage requests
"""

import os
import sys
import re
import time
import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pathlib import Path

def install_if_missing(package, import_name=None):
    try:
        __import__(import_name or package)
    except ImportError:
        print(f"   Installing {package}...")
        ret = os.system(f'{sys.executable} -m pip install {package} --break-system-packages -q')
        if ret != 0:
            os.system(f'{sys.executable} -m pip install {package} -q')

print("\n🔧 Checking dependencies...")
for pkg, imp in [
    ("DrissionPage", "DrissionPage"),
    ("requests",     "requests"),
]:
    install_if_missing(pkg, imp)
print("   All dependencies ready.\n")

from DrissionPage import ChromiumPage, ChromiumOptions

def _detect_chromium() -> tuple[str, str]:
    """
    Find the Chromium binary and return (binary_path, version_string).
    Returns ("", "135.0.0.0") if nothing is found (version is a safe fallback).
    """
    import subprocess
    import shutil

    candidates = [
        "chromium-browser",
        "chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
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
            m = re.search(r'[\d]+\.[\d.]+', out)
            version = m.group(0) if m else "135.0.0.0"
            major = version.split(".")[0]
            ua_version = f"{major}.0.0.0"
            print(f"  🔍 Detected Chromium {version} → UA version {ua_version}")
            return resolved, ua_version
        except Exception:
            continue

    print("   Could not detect Chromium version — using fallback UA version.")
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
CLOUDFLARE_WAIT    = 10
PAGE_AJAX_WAIT     = 8

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
                print(f"\n       Failed: {e}")
            else:
                time.sleep(2)
    return False

def create_browser() -> ChromiumPage:
    """
    Create a DrissionPage ChromiumPage instance.
    DrissionPage drives a REAL Chromium window (not headless) which
    passes Cloudflare's JS/TLS fingerprint checks.
    The Chromium binary and its version are auto-detected at startup.
    """
    opts = ChromiumOptions()

    if _CHROMIUM_BIN:
        opts.set_browser_path(_CHROMIUM_BIN)
        print(f"  🌐 Using Chromium binary: {_CHROMIUM_BIN}")
    else:
        print("  ⚠️  Chromium not found in common paths — falling back to default browser.")

    opts.headless(False)

    opts.set_argument("--no-sandbox")
    opts.set_argument("--disable-dev-shm-usage")
    opts.set_argument("--mute-audio")
    opts.set_argument("--window-size=1280,800")

    page = ChromiumPage(opts)
    page.set.timeouts(page_load=40, script=20)

    HOOK_SCRIPT = "window.__mf_hooked=true; window.__mf_log=[];const _ft=window.fetch; window.fetch=function(){const u=(arguments[0]&&arguments[0].url)?arguments[0].url:String(arguments[0]);return _ft.apply(this,arguments).then(function(r){r.clone().text().then(function(t){window.__mf_log.push({url:u,body:t});}).catch(function(){});return r;});};const _op=XMLHttpRequest.prototype.open,_sd=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.open=function(m,u){this.__u=u;return _op.apply(this,arguments);};XMLHttpRequest.prototype.send=function(){this.addEventListener('load',function(){try{window.__mf_log.push({url:this.__u,body:this.responseText});}catch(e){}});return _sd.apply(this,arguments);};"
    try:
        page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=HOOK_SCRIPT)
    except Exception:
        try:
            page.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_SCRIPT})
        except Exception:
            pass

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

def enable_network_logging(page: ChromiumPage):
    """Enable CDP Network domain so we can capture response bodies."""
    try:
        page.run_cdp("Network.enable")
    except Exception:
        pass

INTERCEPT_JS = """
if (!window.__mf_hooked) {
    window.__mf_hooked = true;
    window.__mf_log = [];

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
        try:
            data = json.loads(body)
            images.extend(walk_json_for_images(data))
        except Exception:
            pass
        for m in re.finditer(r'https?://\S+?\.(?:jpe?g|png|webp)(?:\?\S*)?', body, re.I):
            images.append(m.group(0).strip('"').strip("'").strip("\\"))
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

def get_chapter_list(page: ChromiumPage, slug: str) -> list:
    info_url = f"https://mangafire.to/manga/{slug}"
    print(f"   Navigating to: {info_url}")

    page.get(info_url)
    wait_for_cloudflare(page)
    inject_hooks(page)

    time.sleep(PAGE_AJAX_WAIT)

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

    try:
        for el in page.eles("tag:a"):
            href = el.attr("href") or ""
            m = re.search(r'/chapter-([\d.]+)', href)
            if m:
                chapters.add(float(m.group(1)))
    except Exception:
        pass

    try:
        src = page.html or ""
        for m in re.finditer(r'/chapter-([\d.]+)', src):
            chapters.add(float(m.group(1)))
    except Exception:
        pass

    if len(chapters) > 5 and 0.0 in chapters:
        chapters.discard(0.0)

    return sorted(chapters)

def _find_desc_in_json(data, depth=0) -> str:
    if depth > 10:
        return ""
    best = ""
    DESC_KEYS = {"description", "synopsis", "summary", "plot", "about", "overview", "desc", "excerpt"}
    if isinstance(data, dict):
        for k, v in data.items():
            if k.lower() in DESC_KEYS and isinstance(v, str) and len(v) > len(best):
                best = v
            sub = _find_desc_in_json(v, depth + 1)
            if len(sub) > len(best):
                best = sub
    elif isinstance(data, list):
        for x in data:
            sub = _find_desc_in_json(x, depth + 1)
            if len(sub) > len(best):
                best = sub
    return best

def _unescape_html(s: str) -> str:
    return (s.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
             .replace('&quot;', '"').replace('&#039;', "'").replace('&nbsp;', ' '))

# ══════════════════════════════════════════════════════════════════════════════
#  DESCRIPTION STRATEGIES  — swap them in get_manga_info() below
#  Set DESCRIPTION_STRATEGY = "A" / "B" / "C" / "D" / "E" to pick one.
#  Each strategy returns the full description string, or "" on failure.
# ══════════════════════════════════════════════════════════════════════════════

DESCRIPTION_STRATEGY = "E"

def _click_read_more(page: ChromiumPage) -> str:
    """Click the 'read more' button. Returns a status string for logging."""
    try:
        result = page.run_js("""
            var tags = ['a','button','span','div','p','i','b','strong'];
            for (var t = 0; t < tags.length; t++) {
                var els = document.querySelectorAll(tags[t]);
                for (var i = 0; i < els.length; i++) {
                    var raw = (els[i].textContent || '').replace(/\\s+/g,' ').trim().toLowerCase();
                    if (raw === 'read more' || raw === 'read more +' || raw === 'read more+'
                            || raw === 'show more' || raw === '+ read more'
                            || (raw.startsWith('read more') && raw.length < 20)) {
                        els[i].click();
                        return 'clicked:' + els[i].tagName + '/' + (els[i].className||'') + ' text=' + raw;
                    }
                }
            }
            return 'not_found';
        """) or "not_found"
        return result
    except Exception as e:
        return f"error:{e}"


# ── Strategy A: fingerprint match (DEFAULT) ───────────────────────────────────
# Captures the FULL truncated text first (so we know its exact length),
# clicks read more, then finds an element that:
#   • contains the start of the truncated text (fingerprint)
#   • is STRICTLY LONGER than the full truncated text
#   • does NOT end with "..."
# This makes it impossible to accidentally return the truncated version itself.
def _desc_strategy_A(page: ChromiumPage) -> str:
    # Step 1 — find the truncated description element specifically:
    #   • must end with "..." (CSS line-clamp cutoff)
    #   • must not look like a title (no colons at start, not all-caps, etc.)
    #   • UI noise words must not dominate the text
    full_truncated = ""
    try:
        full_truncated = page.run_js("""
            var UI_NOISE = ['start reading','read now','bookmark','add to list',
                            'sign in','log in','register','send request','more information'];
            function hasNoise(t) {
                var low = t.toLowerCase();
                var hits = 0;
                for (var n = 0; n < UI_NOISE.length; n++) {
                    if (low.indexOf(UI_NOISE[n]) !== -1) hits++;
                }
                return hits >= 2;
            }
            // Walk every element, pick the one that ends with "..." and looks like prose
            var best = '';
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                if (el.children.length > 3) continue;
                var t = (el.textContent||'').replace(/\\s+/g,' ').trim();
                // Must end with the ellipsis the site appends when truncating
                if (t.slice(-3) !== '...' && t.slice(-1) !== '…') continue;
                if (t.length < 40 || t.length > 800) continue;
                if (hasNoise(t)) continue;
                // Prefer longer (more complete) truncated text
                if (t.length > best.length) best = t;
            }
            return best || '';
        """) or ""
    except Exception:
        pass

    if not full_truncated:
        return ""

    trunc_len   = len(full_truncated)
    # Use first 40 chars as fingerprint — enough to be unique, short enough to be safe
    fingerprint = full_truncated[:40].replace("'", "\\'").replace("\\", "\\\\")
    print(f"   [A] Truncated text ({trunc_len} chars): '{full_truncated[:80]}'")

    # Step 2 — click read more
    status = _click_read_more(page)
    print(f"   [A] Click result: {status}")
    time.sleep(3)

    # Step 3 — find element whose text STARTS WITH the fingerprint and is longer
    # "starts with" is the key: the popup starts directly with the description.
    # Page containers have titles/buttons before the description, so they won't match.
    try:
        result = page.run_js(f"""
            var fp = '{fingerprint}';
            var minLen = {trunc_len + 1};
            var best = null;
            var UI_NOISE = ['start reading','read now','bookmark','add to list',
                            'sign in','log in','register','send request'];
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {{
                var el = all[i];
                var cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
                var t = (el.textContent||'').replace(/\\s+/g,' ').trim();
                // MUST start with the fingerprint — not just contain it
                if (t.indexOf(fp) !== 0) continue;
                if (t.length < minLen || t.length > 5000) continue;
                // Reject if still truncated
                if (t.slice(-3) === '...' || t.slice(-1) === '…') continue;
                // Reject UI noise
                var low = t.toLowerCase();
                var noisy = false;
                for (var n = 0; n < UI_NOISE.length; n++) {{
                    if (low.indexOf(UI_NOISE[n]) !== -1) {{ noisy = true; break; }}
                }}
                if (noisy) continue;
                // Take the shortest matching element (avoids grabbing parent wrappers)
                if (best === null || t.length < best.length) best = t;
            }}
            return best;
        """)
        if result:
            print(f"   [A] Hit ({len(result)} chars): {result[:120]}...")
            return result.strip()
    except Exception as e:
        print(f"   [A] Error: {e}")
    return ""


# ── Strategy B: DOM snapshot diff ────────────────────────────────────────────
# Takes a snapshot of all text blocks before clicking, then after clicking
# finds any NEW text blocks that appeared — those must be the popup content.
def _desc_strategy_B(page: ChromiumPage) -> str:
    # Snapshot before click
    try:
        before_texts = set(page.run_js("""
            var out = [];
            var els = document.querySelectorAll('p, span, div, section, article');
            for (var i = 0; i < els.length; i++) {
                var t = (els[i].textContent||'').replace(/\\s+/g,' ').trim();
                if (t.length >= 60) out.push(t);
            }
            return out;
        """) or [])
    except Exception:
        before_texts = set()

    status = _click_read_more(page)
    print(f"   [B] Click result: {status}")
    time.sleep(3)

    # Find new text blocks that weren't there before
    try:
        after_texts = page.run_js("""
            var out = [];
            var els = document.querySelectorAll('p, span, div, section, article');
            for (var i = 0; i < els.length; i++) {
                var cs = window.getComputedStyle(els[i]);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                var t = (els[i].textContent||'').replace(/\\s+/g,' ').trim();
                if (t.length >= 60 && t.length <= 3000) out.push(t);
            }
            return out;
        """) or []
        for t in after_texts:
            if t not in before_texts and t.slice(-3) != '...' if hasattr(t, 'slice') else t[-3:] != '...':
                print(f"   [B] New text found ({len(t)} chars): {t[:120]}...")
                return t.strip()
    except Exception as e:
        print(f"   [B] Error: {e}")
    return ""


# ── Strategy C: data-attribute scan ──────────────────────────────────────────
# MangaFire often stores the full description in a data-* attribute on the
# read-more button or a nearby element. This checks for that before any click.
def _desc_strategy_C(page: ChromiumPage) -> str:
    try:
        result = page.run_js("""
            var all = document.querySelectorAll('*');
            var best = '';
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                for (var a = 0; a < el.attributes.length; a++) {
                    var name = el.attributes[a].name.toLowerCase();
                    var val  = el.attributes[a].value || '';
                    if ((name.startsWith('data-') || name === 'title' || name === 'content')
                            && val.length > 80 && val.length < 5000
                            && !val.startsWith('http') && val.indexOf(' ') !== -1) {
                        if (val.length > best.length) best = val;
                    }
                }
            }
            return best || null;
        """)
        if result and len(result) > 80:
            # Reject login/UI noise
            noise = ['sign in', 'log in', 'register', 'forgot password', 'send request']
            low = result.lower()
            if not any(w in low for w in noise):
                print(f"   [C] Data-attr hit ({len(result)} chars): {result[:120]}...")
                return result.strip()
    except Exception as e:
        print(f"   [C] Error: {e}")
    return ""


# ── Strategy D: raw HTML / JSON-LD / script tag scan ─────────────────────────
# Parses the raw page HTML for description text inside JSON-LD blocks,
# meta tags, or inline script variables — never looks at rendered DOM.
def _desc_strategy_D(page: ChromiumPage) -> str:
    try:
        html = page.html or ""
    except Exception:
        return ""

    # JSON-LD
    for block in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.group(1))
            for key in ('description', 'abstract', 'about'):
                val = data.get(key, "")
                if isinstance(val, str) and len(val) > 80:
                    print(f"   [D] JSON-LD hit ({len(val)} chars): {val[:120]}...")
                    return _unescape_html(val).strip()
        except Exception:
            pass

    # Inline JS variable assignments like: description: "...", 'description': '...'
    for m in re.finditer(
        r'["\']?(?:description|synopsis|summary|overview)["\']?\s*:\s*["\']([^"\']{80,})["\']',
        html, re.I
    ):
        val = _unescape_html(m.group(1).replace('\\n', '\n').replace('\\"', '"')).strip()
        if len(val) > 80 and val[-3:] != '...':
            print(f"   [D] Inline JS hit ({len(val)} chars): {val[:120]}...")
            return val

    # og:description / meta description
    for pattern in [
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:description["\']',
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
    ]:
        m = re.search(pattern, html, re.I | re.S)
        if m:
            val = _unescape_html(m.group(1).strip())
            if len(val) >= 80:
                print(f"   [D] Meta tag hit ({len(val)} chars): {val[:120]}...")
                return val
    return ""


# ── Strategy E: AJAX network log ─────────────────────────────────────────────
# Looks through intercepted XHR/fetch responses for a description field.
# Works best if the page loads description data via an API call.
def _desc_strategy_E(page: ChromiumPage) -> str:
    try:
        log = get_log(page)
    except Exception:
        return ""
    best = ""
    for entry in (log if isinstance(log, list) else []):
        body = entry.get("body", "") if isinstance(entry, dict) else ""
        if not body or len(body) < 80:
            continue
        try:
            data = json.loads(body)
            found = _find_desc_in_json(data)
            if found and len(found) > len(best):
                best = found.strip()
        except Exception:
            pass
        for m in re.finditer(
            r'"(?:description|synopsis|summary|overview|about|plot|excerpt)"\s*:\s*"((?:[^"\\]|\\.){80,})"',
            body, re.I
        ):
            val = m.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\').strip()
            if len(val) > len(best):
                best = val
    if best:
        print(f"   [E] AJAX hit ({len(best)} chars): {best[:120]}...")
    return best


def get_manga_info(page: ChromiumPage) -> tuple:
    cover_url = ""
    description = ""

    try:
        cover_url = page.run_js("""
            var sel = ['.poster img', '.manga-poster img', '.cover img', '.manga-cover img',
                       '[class*="poster"] img', '[class*="cover"] img', 'img[class*="cover"]',
                       'img[class*="poster"]'];
            for (var i = 0; i < sel.length; i++) {
                var el = document.querySelector(sel[i]);
                if (el) {
                    var src = el.getAttribute('src') || el.getAttribute('data-src') || el.getAttribute('data-original') || '';
                    if (src && src.length > 5) return src.startsWith('http') ? src : 'https://mangafire.to' + src;
                }
            }
            return '';
        """) or ""
    except Exception:
        pass

    # Try the selected strategy first, then fall through the others as backups.
    # Change DESCRIPTION_STRATEGY at the top of this section to pick a different one.
    strategy_order = {
        "A": [_desc_strategy_A, _desc_strategy_C, _desc_strategy_D, _desc_strategy_E, _desc_strategy_B],
        "B": [_desc_strategy_B, _desc_strategy_A, _desc_strategy_C, _desc_strategy_D, _desc_strategy_E],
        "C": [_desc_strategy_C, _desc_strategy_A, _desc_strategy_D, _desc_strategy_E, _desc_strategy_B],
        "D": [_desc_strategy_D, _desc_strategy_A, _desc_strategy_C, _desc_strategy_E, _desc_strategy_B],
        "E": [_desc_strategy_E, _desc_strategy_A, _desc_strategy_C, _desc_strategy_D, _desc_strategy_B],
    }
    strategies = strategy_order.get(DESCRIPTION_STRATEGY,
                                    strategy_order["A"])

    for fn in strategies:
        try:
            result = fn(page)
            if result and len(result) > 40:
                description = result
                break
        except Exception as e:
            print(f"   Strategy {fn.__name__} crashed: {e}")

    description = description.strip()
    print(f"   Final description ({DESCRIPTION_STRATEGY}): {len(description)} chars")
    return cover_url, description

def save_cover_and_info(cover_url: str, description: str, manga_info_url: str,
                        save_path: Path, session: requests.Session):
    cover_dir = save_path / "Cover"
    cover_dir.mkdir(parents=True, exist_ok=True)

    if cover_url:
        print(f"   Downloading cover image...")
        ok = download_image(cover_url, cover_dir / "cover.jpg", session)
        if ok:
            print(f"   Cover saved → {cover_dir / 'cover.jpg'}")
        else:
            print(f"   Cover download failed.")

    info_path = cover_dir / "info.txt"
    if description:
        info_path.write_text(description, encoding="utf-8")
        print(f"   Info saved  → {info_path}  ({len(description)} chars)")
    else:
        info_path.write_text("", encoding="utf-8")
        print(f"   ⚠️  Description was empty — info.txt written blank.")

def get_chapter_images(page: ChromiumPage, url: str) -> list:
    clear_log(page)
    page.get(url)
    wait_for_cloudflare(page)

    print(f"   Waiting for image data to load...", end="", flush=True)
    time.sleep(PAGE_AJAX_WAIT)

    images = parse_log_for_images(get_log(page))
    print(f"\r   AJAX log: {len(images)} image URLs captured.          ")

    if len(images) < 5:
        print(f"   Triggering lazy-load via scrollIntoView on all img elements...")
        try:
            try:
                page.actions.key("h")
                time.sleep(0.3)
                page.actions.key("m")
                time.sleep(0.3)
            except Exception:
                pass

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

    EXCLUDE = [r'/logo', r'/icon', r'favicon', r'/avatar', r'\.svg',
               r'data:image', r'mangafire\.to/assets']
    seen, clean = set(), []
    for img in images:
        img = img.strip()
        if img and img not in seen and not any(re.search(p, img, re.I) for p in EXCLUDE):
            seen.add(img)
            clean.append(img)

    return clean

def download_chapter(ch_num, url, manga_root, session, page) -> int:
    folder   = manga_root / chapter_folder_name(ch_num)
    existing = list(folder.glob("*.*"))
    if len(existing) >= 5:
        print(f"    Chapter {fmt_num(ch_num)} already saved ({len(existing)} pages). Skipping.")
        return len(existing)

    MAX_PAGE_ATTEMPTS = 3
    images = []
    for page_attempt in range(1, MAX_PAGE_ATTEMPTS + 1):
        print(f"   Loading chapter {fmt_num(ch_num)} (attempt {page_attempt}/{MAX_PAGE_ATTEMPTS})...", end="", flush=True)
        images = get_chapter_images(page, url)
        print(f"\r   Found {len(images)} pages.           ")
        if images:
            break
        if page_attempt < MAX_PAGE_ATTEMPTS:
            print(f"    No pages found — retrying in 5 seconds...")
            time.sleep(5)
        else:
            print(f"   Chapter {fmt_num(ch_num)} — gave up after {MAX_PAGE_ATTEMPTS} attempts.")

    if not images:
        return 0

    folder.mkdir(parents=True, exist_ok=True)
    success = 0
    consec_failures = 0
    MAX_CONSEC_FAILURES = 3
    for i, img_url in enumerate(images, 1):
        ext_m = re.search(r'\.(jpe?g|png|webp)', img_url, re.I)
        ext   = ext_m.group(0).lower() if ext_m else ".jpg"
        if ext == ".jpeg":
            ext = ".jpg"
        dest = folder / f"page_{i:03d}{ext}"
        if download_image(img_url, dest, session):
            success += 1
            consec_failures = 0
        else:
            consec_failures += 1
            if consec_failures >= MAX_CONSEC_FAILURES:
                print(f"\r    {MAX_CONSEC_FAILURES} consecutive failures — skipping chapter.            ")
                return 0
        sys.stdout.write(f"\r   {success}/{len(images)} pages saved...   ")
        sys.stdout.flush()

    status = "✅" if success == len(images) else "⚠️"
    print(f"\r  {status} Chapter {fmt_num(ch_num)} — {success}/{len(images)} pages.            ")
    return success

def prompt_single_manga(index: int, total: int, default_location: str) -> dict:
    """Prompt the user for one manga's details."""
    print(f"\n── Manga {index}/{total} " + "─" * 36)

    while True:
        url = input("Set link (chapter number auto-increments):\n  → ").strip()
        if re.search(r'mangafire\.to/read/.+/chapter-[\d.]+', url):
            break
        print("  Must be a MangaFire reader URL, e.g.:\n"
              "  https://mangafire.to/read/grand-bluee.lxx3/en/chapter-0\n")

    raw_name    = input("\nFolder name (e.g. Grand Blue Dreaming):\n  → ").strip()
    folder_name = sanitize(raw_name) or f"Manga_{index}"

    print(f"\nLocation (blank = same as manga 1 / current dir [{default_location}]):")
    location  = input("  → ").strip() or default_location
    save_path = Path(location) / folder_name

    return {"url": url, "folder_name": folder_name, "save_path": save_path}

def prompt_all_mangas() -> list[dict]:
    """Ask how many mangas, then collect details for each one upfront."""
    print("╔══════════════════════════════════════════════╗")
    print("║       MangaFire.to — Manga Downloader        ║")
    print("╚══════════════════════════════════════════════╝\n")

    while True:
        try:
            count = int(input("How many mangas do you want to download?\n  → ").strip())
            if count >= 1:
                break
            print("    Please enter a number ≥ 1.")
        except ValueError:
            print("    Please enter a valid number.")

    print(f"\n  Enter details for all {count} manga(s) now — downloads start after.\n")

    mangas = []
    default_location = os.getcwd()

    for i in range(1, count + 1):
        cfg = prompt_single_manga(i, count, default_location)
        mangas.append(cfg)
        if i == 1:
            default_location = str(cfg["save_path"].parent)

    print("\n" + "═" * 54)
    print(" Download queue:")
    for i, m in enumerate(mangas, 1):
        print(f"  {i}. {m['folder_name']}")
        print(f"      {m['save_path']}")
    print("═" * 54)
    input("\nPress Enter to start downloading...\n")

    return mangas

def download_manga(cfg: dict, page: ChromiumPage, session: requests.Session,
                   manga_index: int, manga_total: int):
    url       = cfg["url"]
    save_path = cfg["save_path"]

    m    = re.search(r'mangafire\.to/read/([^/]+)/([^/]+)/chapter', url)
    slug = m.group(1) if m else ""

    print(f"\n{'═' * 54}")
    print(f" [{manga_index}/{manga_total}] Starting: {cfg['folder_name']}")
    print(f" Save path: {save_path}")
    print("═" * 54)
    save_path.mkdir(parents=True, exist_ok=True)

    print("\n Discovering chapters...")
    chapters = get_chapter_list(page, slug)

    cover_url, description = get_manga_info(page)
    manga_info_url = f"https://mangafire.to/manga/{slug}"
    save_cover_and_info(cover_url, description, manga_info_url, save_path, session)

    if not chapters:
        start    = parse_chapter_number(url)
        chapters = [start]
        print(f"    Could not get chapter list. Trying from chapter {fmt_num(start)}.")
    else:
        print(f"   Found {len(chapters)} chapters: "
              f"{fmt_num(min(chapters))} → {fmt_num(max(chapters))}")

    total = len(chapters)
    print(f"\n Saving all {total} chapters — this may take a while...\n")

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
            print(f"   Error: {e}")
            failed.append(ch_num)
        time.sleep(BETWEEN_CHAPTERS)

    last = chapters[-1] if chapters else "?"
    print("\n" + "─" * 54)
    print(f" [{manga_index}/{manga_total}] Finished: {cfg['folder_name']}")
    print(f"   Saved {downloaded}/{total} chapters")
    print(f"   ({cfg['folder_name']} has {fmt_num(last)} chapters)")
    print(f"    Location: {save_path}")
    if failed:
        print(f"     Failed chapters: {', '.join(fmt_num(c) for c in failed)}")
    print("─" * 54)

    return downloaded, total, failed

def main():
    mangas = prompt_all_mangas()

    session = requests.Session()
    session.headers.update(DOWNLOAD_HEADERS)
    _no_retry = HTTPAdapter(max_retries=Retry(total=0, raise_on_status=False))
    session.mount("https://", _no_retry)
    session.mount("http://",  _no_retry)

    print("\n Opening Chromium (visible window — needs to pass Cloudflare)...")
    print("   The browser window will open on your screen. Don't close it!\n")
    page = create_browser()

    overall_results = []

    try:
        for idx, cfg in enumerate(mangas, 1):
            downloaded, total, failed = download_manga(
                cfg, page, session, idx, len(mangas)
            )
            overall_results.append({
                "name":       cfg["folder_name"],
                "downloaded": downloaded,
                "total":      total,
                "failed":     failed,
                "path":       cfg["save_path"],
            })

    finally:
        page.quit()

    print("\n" + "═" * 54)
    print(f"🏁 All done! Downloaded {len(mangas)} manga(s):\n")
    for r in overall_results:
        status = "✅" if not r["failed"] else "⚠️"
        print(f"  {status} {r['name']}")
        print(f"     Chapters: {r['downloaded']}/{r['total']}")
        print(f"      {r['path']}")
        if r["failed"]:
            print(f"      Failed: {', '.join(fmt_num(c) for c in r['failed'])}")
    print("═" * 54 + "\n")

    any_failed = any(r["failed"] for r in overall_results)
    if any_failed:
        print("📝 Writing notsaved.txt files for failed chapters...\n")
        for r in overall_results:
            if not r["failed"]:
                continue

            cfg_for_r = next(
                (m for m in mangas if str(m["save_path"]) == str(r["path"])), None
            )
            base_url = cfg_for_r["url"] if cfg_for_r else "<unknown url>"

            lines = []
            lines.append(f"Manga:  {r['name']}")
            lines.append(f"Folder: {r['path']}")
            lines.append(f"Base URL: {base_url}")
            lines.append("")
            lines.append(f"Not downloaded ({len(r['failed'])} chapters):")
            lines.append("─" * 50)
            for ch_num in sorted(r["failed"]):
                ch_link = chapter_url(base_url, ch_num)
                lines.append(f"  Chapter {fmt_num(ch_num):>8}  →  {ch_link}")
            lines.append("")

            notsaved_path = Path(r["path"]) / "notsaved.txt"
            notsaved_path.parent.mkdir(parents=True, exist_ok=True)
            notsaved_path.write_text("\n".join(lines), encoding="utf-8")

            print(f"   {r['name']}")
            print(f"      Folder : {r['path']}")
            print(f"      Saved  : {notsaved_path}")
            print(f"      Missing: {', '.join(fmt_num(c) for c in sorted(r['failed']))}\n")
    else:
        print("✅ No failed chapters — no notsaved.txt needed.\n")

    print("    All done. Shutting down in 15 seconds...")
    print("   (Close this window or press Ctrl+C to cancel shutdown)\n")
    try:
        time.sleep(15)
    except KeyboardInterrupt:
        print("\n🛑 Shutdown cancelled.")
        return

    if sys.platform.startswith("win"):
        os.system("shutdown /s /t 0")
    else:
        os.system("shutdown -h now")


if __name__ == "__main__":
    main()