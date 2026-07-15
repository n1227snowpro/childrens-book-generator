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
                pdf_url TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL,
                page_num INTEGER NOT NULL,
                s3_url TEXT,
                story_text TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)


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


def create_book(book_id, title, page_count, status="running"):
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO books (book_id, title, page_count, pdf_url, created_at, status) VALUES (?, ?, ?, NULL, ?, ?)",
            (book_id, title, page_count, _now(), status),
        )


def update_book(book_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [book_id]
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE books SET {cols} WHERE book_id = ?", values)


def add_page(book_id, page_num, s3_url, story_text):
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO pages (book_id, page_num, s3_url, story_text) VALUES (?, ?, ?, ?)",
            (book_id, page_num, s3_url, story_text),
        )


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
            "SELECT page_num, s3_url, story_text FROM pages WHERE book_id = ? ORDER BY page_num", (book_id,)
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
