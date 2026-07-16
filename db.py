import json
import sqlite3
import threading
from datetime import datetime, timezone

from config import DB_PATH

_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _strip_url_to_key(url):
    """Legacy rows stored a full (broken) R2 endpoint URL where an object key belongs.
    An object key is everything after the host, e.g. "books/<id>/final/x.pdf"."""
    parts = url.split("/", 3)
    return parts[3] if len(parts) == 4 else url


def _repair_legacy_object_urls(conn):
    for row in conn.execute("SELECT book_id, pdf_key FROM books WHERE pdf_key LIKE 'http%'").fetchall():
        conn.execute(
            "UPDATE books SET pdf_key = ? WHERE book_id = ?",
            (_strip_url_to_key(row["pdf_key"]), row["book_id"]),
        )
    for row in conn.execute("SELECT id, s3_key FROM pages WHERE s3_key LIKE 'http%'").fetchall():
        conn.execute(
            "UPDATE pages SET s3_key = ? WHERE id = ?",
            (_strip_url_to_key(row["s3_key"]), row["id"]),
        )


def init_db():
    with _lock, get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                step TEXT,
                current INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                book_id TEXT,
                pdf_url TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS books (
                book_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                pdf_key TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                image_model TEXT,
                cover_key TEXT,
                target_age TEXT,
                theme TEXT,
                content_instruction TEXT,
                main_characters TEXT,
                art_style_preference TEXT,
                blueprint_json TEXT,
                art_style_ref_key TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE books ADD COLUMN image_model TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE books ADD COLUMN cover_key TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE books RENAME COLUMN pdf_url TO pdf_key")
        except sqlite3.OperationalError:
            pass
        for _col in ("target_age", "theme", "content_instruction", "main_characters",
                     "art_style_preference", "blueprint_json", "art_style_ref_key"):
            try:
                conn.execute(f"ALTER TABLE books ADD COLUMN {_col} TEXT")
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL,
                page_num INTEGER NOT NULL,
                s3_key TEXT,
                story_text TEXT,
                image_prompt TEXT,
                is_placeholder INTEGER DEFAULT 0
            )
        """)
        try:
            conn.execute("ALTER TABLE pages RENAME COLUMN s3_url TO s3_key")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE pages ADD COLUMN image_prompt TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE pages ADD COLUMN is_placeholder INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                visual_description TEXT,
                personality TEXT,
                role TEXT,
                image_prompt TEXT,
                s3_key TEXT,
                image_model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_characters_name_key ON characters(name_key)")
        _repair_legacy_object_urls(conn)


def _now():
    return datetime.now(timezone.utc).isoformat()


def create_job(job_id):
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, status, step, current, total, created_at) VALUES (?, 'queued', '', 0, 0, ?)",
            (job_id, _now()),
        )


def update_job(job_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE job_id = ?", values)


def get_job(job_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def create_book(
    book_id, title, page_count, status="running", image_model=None,
    target_age=None, theme=None, content_instruction=None, main_characters=None,
    art_style_preference=None, blueprint_json=None,
):
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO books (book_id, title, page_count, pdf_key, created_at, status, image_model, "
            "target_age, theme, content_instruction, main_characters, art_style_preference, blueprint_json) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                book_id, title, page_count, _now(), status, image_model,
                target_age, theme, content_instruction, main_characters,
                art_style_preference, blueprint_json,
            ),
        )


def update_book(book_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [book_id]
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE books SET {cols} WHERE book_id = ?", values)


def add_page(book_id, page_num, s3_key, story_text, image_prompt=None, is_placeholder=False):
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO pages (book_id, page_num, s3_key, story_text, image_prompt, is_placeholder) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (book_id, page_num, s3_key, story_text, image_prompt, int(is_placeholder)),
        )


def update_page(book_id, page_num, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [book_id, page_num]
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE pages SET {cols} WHERE book_id = ? AND page_num = ?", values)


def get_page(book_id, page_num):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pages WHERE book_id = ? AND page_num = ?", (book_id, page_num)
        ).fetchone()
        return dict(row) if row else None


def list_books():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_book(book_id):
    with get_conn() as conn:
        book = conn.execute("SELECT * FROM books WHERE book_id = ?", (book_id,)).fetchone()
        if not book:
            return None
        book = dict(book)
        pages = conn.execute(
            "SELECT page_num, s3_key, story_text, image_prompt, is_placeholder FROM pages "
            "WHERE book_id = ? ORDER BY page_num", (book_id,)
        ).fetchall()
        book["pages"] = [dict(p) for p in pages]
        return book


def delete_book(book_id):
    with _lock, get_conn() as conn:
        conn.execute("DELETE FROM books WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM pages WHERE book_id = ?", (book_id,))


def get_setting(key):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key, value):
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _name_key(name):
    return (name or "").strip().lower()


def get_character_by_name(name):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE name_key = ?", (_name_key(name),)
        ).fetchone()
        return dict(row) if row else None


def upsert_character(name, visual_description, personality, role, image_prompt, s3_key, image_model):
    now = _now()
    name_key = _name_key(name)
    with _lock, get_conn() as conn:
        existing = conn.execute("SELECT id FROM characters WHERE name_key = ?", (name_key,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE characters SET name = ?, visual_description = ?, personality = ?, role = ?, "
                "image_prompt = ?, s3_key = ?, image_model = ?, updated_at = ? WHERE id = ?",
                (name, visual_description, personality, role, image_prompt, s3_key, image_model, now, existing["id"]),
            )
            return existing["id"]
        cursor = conn.execute(
            "INSERT INTO characters (name, name_key, visual_description, personality, role, "
            "image_prompt, s3_key, image_model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, name_key, visual_description, personality, role, image_prompt, s3_key, image_model, now, now),
        )
        return cursor.lastrowid


def list_characters():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM characters ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_character(character_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
        return dict(row) if row else None


def delete_character(character_id):
    with _lock, get_conn() as conn:
        conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))
