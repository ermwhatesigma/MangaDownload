"""
╔══════════════════════════════════════════════╗
║         Manga Home Server                    ║
║  Flask streaming server for local manga      ║
╚══════════════════════════════════════════════╝

Usage:
    python3 server.py

Then open http://<your-ip>:8000 on any device on your WiFi.
"""

import os
import re
import json
import sqlite3
import hashlib
import random
import mimetypes
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template, redirect, url_for,
    session, request, send_file, jsonify, abort, g,
)

app = Flask(__name__)
app.secret_key = "mangafire_sigma_secret_9182736"

@app.template_filter('shuffle_sample')
def shuffle_sample_filter(lst, n=2):
    """Return n random items from lst, seeded by today's date + a small offset."""
    seed = int(datetime.now().strftime("%Y%m%d")) + 77
    rng  = random.Random(seed)
    return rng.sample(list(lst), min(n, len(lst)))

BASE_DIR   = Path(__file__).parent
MANGA_ROOT = BASE_DIR / "mangas"
DB_PATH    = BASE_DIR / "main.db"

USERS = {
    "Your_username": hashlib.sha256("Your_password".encode()).hexdigest()
}


# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS reading_progress (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            manga       TEXT NOT NULL,
            chapter     TEXT NOT NULL,
            page        INTEGER DEFAULT 1,
            total_pages INTEGER DEFAULT 1,
            updated_at  TEXT NOT NULL,
            UNIQUE(manga, chapter)
        );

        CREATE TABLE IF NOT EXISTS reading_list (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            manga      TEXT NOT NULL UNIQUE,
            status     TEXT DEFAULT 'reading',
            added_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS last_read (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            manga      TEXT NOT NULL,
            chapter    TEXT NOT NULL,
            page       INTEGER DEFAULT 1,
            read_at    TEXT NOT NULL,
            UNIQUE(manga, chapter)
        );
    """)
    
    cur = db.execute("PRAGMA table_info(last_read)")
    cols = [r[1] for r in cur.fetchall()]
    if cols:
        idx = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='last_read'"
        ).fetchall()
        idx_names = [r[0] for r in idx]
        has_unique = any('manga' in n or 'unique' in n.lower() or 'sqlite_autoindex' in n
                         for n in idx_names)
        if not has_unique:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS last_read_new (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    manga      TEXT NOT NULL,
                    chapter    TEXT NOT NULL,
                    page       INTEGER DEFAULT 1,
                    read_at    TEXT NOT NULL,
                    UNIQUE(manga, chapter)
                );
                INSERT OR REPLACE INTO last_read_new (id, manga, chapter, page, read_at)
                    SELECT id, manga, chapter, page, read_at FROM last_read;
                DROP TABLE last_read;
                ALTER TABLE last_read_new RENAME TO last_read;
            """)
    db.commit()
    db.close()

MANGA_ROOT.mkdir(exist_ok=True)
init_db()


# ─── Auth ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ─── Manga helpers ────────────────────────────────────────────────────────────

def natural_sort_key(s):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(s))]

def is_extra_chapter(name):
    """Return True if the chapter name contains a decimal number like 008.5 or Chapter 8.5."""
    return bool(re.search(r'\d+\.\d+', str(name)))

def get_all_manga():
    if not MANGA_ROOT.exists():
        return []
    mangas = []
    for d in sorted(MANGA_ROOT.iterdir()):
        if d.is_dir():
            chapters = get_chapters(d.name)
            cover    = get_cover(d.name)
            extras   = sum(1 for c in chapters if is_extra_chapter(c.name))
            mangas.append({
                "name":     d.name,
                "chapters": len(chapters) - extras,
                "extras":   extras,
                "cover":    cover,
            })
    return mangas

_MANGA_SKIP_DIRS = {"cover", "covers", "artwork", "metadata", ".thumb", "thumbs"}

def get_chapters(manga_name):
    manga_dir = MANGA_ROOT / manga_name
    if not manga_dir.exists():
        return []
    # Never treat reserved system folders (cover, artwork, etc.) as chapters
    chapters = [
        d for d in manga_dir.iterdir()
        if d.is_dir() and d.name.lower() not in _MANGA_SKIP_DIRS
    ]
    return sorted(chapters, key=lambda x: natural_sort_key(x.name))

def get_pages(manga_name, chapter_name):
    chapter_dir = MANGA_ROOT / manga_name / chapter_name
    if not chapter_dir.exists():
        return []
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    pages = [f for f in chapter_dir.iterdir() if f.suffix.lower() in exts]
    return sorted(pages, key=lambda x: natural_sort_key(x.name))

def get_cover(manga_name):
    """Return URL to cover.jpg if present, else first page of first chapter."""
    manga_dir = MANGA_ROOT / manga_name
    # Check for a cover file inside the cover/ subfolder
    for cover_name in ["cover.jpg", "cover.jpeg", "cover.png", "cover.webp"]:
        cover_path = manga_dir / "cover" / cover_name
        if cover_path.exists():
            return url_for("serve_cover", manga=manga_name, filename="cover/" + cover_name)
    # Fallback: first page of first chapter
    chapters = get_chapters(manga_name)
    if not chapters:
        return None
    pages = get_pages(manga_name, chapters[0].name)
    if not pages:
        return None
    return url_for("serve_page",
                   manga=manga_name,
                   chapter=chapters[0].name,
                   filename=pages[0].name)


def get_manga_info(manga_name):
    """
    Read info.txt from mangas/<name>/cover/info.txt (or mangas/<name>/info.txt).
    Parses labeled fields; everything else becomes the description.

    Supported fields (all optional):
        Title, Genre/Genres, Author, Artist, Year, Status,
        Rating, Publisher, Type, Volumes, Source
    """
    for info_path in [
        MANGA_ROOT / manga_name / "cover" / "info.txt",
        MANGA_ROOT / manga_name / "info.txt",
    ]:
        if info_path.exists():
            break
    else:
        return {}

    raw = info_path.read_text(encoding="utf-8", errors="replace")

    field_keys = {
        "title", "genre", "genres", "author", "artist",
        "year", "status", "rating", "publisher", "type",
        "volumes", "source", "demographic",
    }

    fields, desc_lines = {}, []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            desc_lines.append("")
            continue
        colon = stripped.find(":")
        if colon > 0:
            key = stripped[:colon].strip().lower()
            val = stripped[colon + 1:].strip()
            if key in field_keys:
                fields[key] = val
                continue
        desc_lines.append(stripped)

    # Trim blank lines from description edges
    while desc_lines and not desc_lines[0]:
        desc_lines.pop(0)
    while desc_lines and not desc_lines[-1]:
        desc_lines.pop()

    fields["description"] = "\n".join(desc_lines) if desc_lines else ""

    # Normalise genre list
    genre_raw = fields.get("genre") or fields.get("genres") or ""
    fields["genre_list"] = [g.strip() for g in re.split(r"[,/|]", genre_raw) if g.strip()]

    return fields

def get_progress(db, manga, chapter=None):
    if chapter:
        row = db.execute(
            "SELECT * FROM reading_progress WHERE manga=? AND chapter=?",
            (manga, chapter)
        ).fetchone()
        return dict(row) if row else None
    # Return latest progress for manga
    row = db.execute(
        "SELECT * FROM reading_progress WHERE manga=? ORDER BY updated_at DESC LIMIT 1",
        (manga,)
    ).fetchone()
    return dict(row) if row else None


# ─── Routes: Auth ──────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        pw_hash  = hashlib.sha256(password.encode()).hexdigest()
        if username in USERS and USERS[username] == pw_hash:
            session["logged_in"] = True
            session["username"]  = username
            return redirect(url_for("index"))
        error = "Invalid credentials."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Routes: Main index ────────────────────────────────────────────────────────

@app.route("/home")
@login_required
def index():
    db     = get_db()
    mangas = get_all_manga()

    # Reading list status map
    rl_rows = db.execute("SELECT manga, status FROM reading_list").fetchall()
    rl_map  = {r["manga"]: r["status"] for r in rl_rows}

    # Attach status + progress to each manga
    for m in mangas:
        m["status"]   = rl_map.get(m["name"], None)
        prog = get_progress(db, m["name"])
        m["progress"] = prog

    # Recently read (last 10 distinct manga)
    recent_rows = db.execute("""
        SELECT manga, chapter, page, read_at FROM last_read
        GROUP BY manga ORDER BY read_at DESC LIMIT 10
    """).fetchall()
    recent = [dict(r) for r in recent_rows]

    # Daily recommended — 6 random mangas (seeded by date so it's stable per day)
    seed = int(datetime.now().strftime("%Y%m%d"))
    rng  = random.Random(seed)
    recommended = rng.sample(mangas, min(6, len(mangas)))

    # Search
    query = request.args.get("q", "").strip().lower()
    if query:
        mangas = [m for m in mangas if query in m["name"].lower()]

    return render_template("index.html",
                           mangas=mangas,
                           recent=recent,
                           recommended=recommended,
                           query=query)


# ─── Routes: Manga detail ──────────────────────────────────────────────────────

@app.route("/manga/<path:manga>")
@login_required
def manga_detail(manga):
    db       = get_db()
    chapters = get_chapters(manga)
    if not (MANGA_ROOT / manga).exists():
        abort(404)

    # Build chapter list with progress
    chapter_list = []
    read_count   = 0
    in_prog_count = 0
    for ch in chapters:
        pages    = get_pages(manga, ch.name)
        prog     = get_progress(db, manga, ch.name)
        pct      = 0
        if prog and prog.get("total_pages"):
            pct = int((prog["page"] / prog["total_pages"]) * 100)
        if pct >= 95:
            read_count += 1
        elif pct > 3:
            in_prog_count += 1
        chapter_list.append({
            "name":     ch.name,
            "pages":    len(pages),
            "progress": prog,
            "pct":      pct,
            "is_extra": is_extra_chapter(ch.name),
        })

    # Reading list status
    rl     = db.execute("SELECT status FROM reading_list WHERE manga=?", (manga,)).fetchone()
    status = rl["status"] if rl else None

    # Resume: last chapter with partial progress
    last = get_progress(db, manga)

    # Total pages across all chapters (for stats)
    total_pages = sum(ch["pages"] for ch in chapter_list)
    extras      = sum(1 for ch in chapter_list if ch["is_extra"])

    info = get_manga_info(manga)

    return render_template("manga_detail.html",
                           manga=manga,
                           chapters=chapter_list,
                           status=status,
                           last=last,
                           cover=get_cover(manga),
                           info=info,
                           read_count=read_count,
                           in_progress=in_prog_count,
                           total_pages=total_pages,
                           extras=extras)


# ─── Routes: Reader ────────────────────────────────────────────────────────────

@app.route("/read/<path:manga>/<path:chapter>")
@login_required
def reader(manga, chapter):
    db      = get_db()
    pages   = get_pages(manga, chapter)
    if not pages:
        abort(404)

    chapters    = get_chapters(manga)
    ch_names    = [c.name for c in chapters]
    ch_index    = ch_names.index(chapter) if chapter in ch_names else 0
    prev_ch     = ch_names[ch_index - 1] if ch_index > 0 else None
    next_ch     = ch_names[ch_index + 1] if ch_index < len(ch_names) - 1 else None

    # Saved page position
    prog        = get_progress(db, manga, chapter)
    start_page  = (prog["page"] if prog else 1)

    # Build page URL list
    page_urls = [
        url_for("serve_page", manga=manga, chapter=chapter, filename=p.name)
        for p in pages
    ]

    # Save to last_read
    db.execute("DELETE FROM last_read WHERE manga=? AND chapter=?", (manga, chapter))
    db.execute("""
        INSERT INTO last_read (manga, chapter, page, read_at)
        VALUES (?, ?, ?, ?)
    """, (manga, chapter, start_page, datetime.now().isoformat()))
    db.execute("""
        INSERT INTO reading_progress (manga, chapter, page, total_pages, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(manga, chapter) DO UPDATE SET
            page=excluded.page,
            total_pages=excluded.total_pages,
            updated_at=excluded.updated_at
    """, (manga, chapter, start_page, len(pages), datetime.now().isoformat()))
    db.commit()

    # Build chapter list with per-chapter progress for the in-reader panel
    ch_list = []
    for ch in chapters:
        prog_ch = get_progress(db, manga, ch.name)
        pct = 0
        if prog_ch and prog_ch.get("total_pages"):
            pct = int((prog_ch["page"] / prog_ch["total_pages"]) * 100)
        ch_list.append({
            "name": ch.name,
            "pct":  pct,
            "read": pct >= 95,
        })

    return render_template("reader.html",
                           manga=manga,
                           chapter=chapter,
                           page_urls=page_urls,
                           start_page=start_page,
                           prev_ch=prev_ch,
                           next_ch=next_ch,
                           ch_index=ch_index,
                           total_chapters=len(chapters),
                           ch_list=ch_list)


# ─── Routes: Save progress (AJAX) ─────────────────────────────────────────────

@app.route("/api/progress", methods=["POST"])
@login_required
def save_progress():
    data    = request.json
    manga   = data.get("manga")
    chapter = data.get("chapter")
    page    = int(data.get("page", 1))
    total   = int(data.get("total", 1))
    db      = get_db()
    db.execute("""
        INSERT INTO reading_progress (manga, chapter, page, total_pages, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(manga, chapter) DO UPDATE SET
            page=excluded.page,
            total_pages=excluded.total_pages,
            updated_at=excluded.updated_at
    """, (manga, chapter, page, total, datetime.now().isoformat()))
    db.execute("DELETE FROM last_read WHERE manga=? AND chapter=?", (manga, chapter))
    db.execute("""
        INSERT INTO last_read (manga, chapter, page, read_at)
        VALUES (?, ?, ?, ?)
    """, (manga, chapter, page, datetime.now().isoformat()))
    db.commit()
    return jsonify({"ok": True})


# ─── Routes: Reading list ──────────────────────────────────────────────────────

@app.route("/reading-list")
@login_required
def reading_list():
    db   = get_db()
    rows = db.execute("""
        SELECT rl.*, rp.chapter, rp.page, rp.total_pages, rp.updated_at as last_read
        FROM reading_list rl
        LEFT JOIN reading_progress rp ON rl.manga = rp.manga
        WHERE rp.updated_at = (
            SELECT MAX(updated_at) FROM reading_progress WHERE manga = rl.manga
        ) OR rp.manga IS NULL
        ORDER BY rl.added_at DESC
    """).fetchall()

    items = []
    for r in rows:
        m = dict(r)
        m["cover"] = get_cover(r["manga"])
        items.append(m)

    return render_template("reading_list.html", items=items, reading=items)

@app.route("/api/reading-list", methods=["POST"])
@login_required
def update_reading_list():
    data   = request.json
    manga  = data.get("manga")
    status = data.get("status")  # reading / completed / plan / remove
    db     = get_db()
    if status == "remove":
        db.execute("DELETE FROM reading_list WHERE manga=?", (manga,))
    else:
        db.execute("""
            INSERT INTO reading_list (manga, status, added_at)
            VALUES (?, ?, ?)
            ON CONFLICT(manga) DO UPDATE SET status=excluded.status
        """, (manga, status, datetime.now().isoformat()))
    db.commit()
    return jsonify({"ok": True})


# ─── Routes: Recently read ─────────────────────────────────────────────────────

@app.route("/recent")
@login_required
def recent():
    db   = get_db()
    rows = db.execute("""
        SELECT lr.manga, lr.chapter, lr.page, lr.read_at,
               rp.total_pages
        FROM last_read lr
        LEFT JOIN reading_progress rp ON lr.manga=rp.manga AND lr.chapter=rp.chapter
        WHERE lr.read_at = (
            SELECT MAX(read_at) FROM last_read WHERE manga = lr.manga
        )
        ORDER BY lr.read_at DESC LIMIT 50
    """).fetchall()
    items = []
    for r in rows:
        m = dict(r)
        m["cover"] = get_cover(r["manga"])
        items.append(m)
    today = datetime.now().strftime("%Y-%m-%d")
    return render_template("recent.html", items=items, today=today)


# ─── Routes: Static image serving ────────────────────────────────────────────

@app.route("/cover/<path:manga>/<path:filename>")
@login_required
def serve_cover(manga, filename):
    path = MANGA_ROOT / manga / filename
    if not path.exists():
        abort(404)
    return send_file(path)


@app.route("/img/<path:manga>/<path:chapter>/<filename>")
@login_required
def serve_page(manga, chapter, filename):
    path = MANGA_ROOT / manga / chapter / filename
    if not path.exists():
        abort(404)
    return send_file(path)

def _extra_group_type(name):
    """Return a short type tag: ova | special | extra | bonus."""
    n = name.strip().lower()
    if n in ("ova", "oav"):       return "ova"
    if re.match(r'^(sp|special)', n): return "special"
    if n in ("bonus",):           return "bonus"
    return "extra"

if __name__ == "__main__":
    MANGA_ROOT.mkdir(exist_ok=True)
    init_db()
    print("\n╔══════════════════════════════════════════════╗")
    print("║         Manga Home Server                    ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  📂 Manga folder : {MANGA_ROOT}")
    print(f"  🗄️  Database     : {DB_PATH}")
    print(f"  🌐 Open on phone: http://<your-ip>:8000")
    print(f"  🔑 Login        : Your_username / Your_password\n")
    app.run(host="0.0.0.0", port=8000, debug=False)