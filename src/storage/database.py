"""SQLite 数据库操作 — 去重记录 + 运行日志"""
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional


DB_PATH = Path(__file__).resolve().parent.parent.parent / "info" / "agent.db"


def get_db() -> sqlite3.Connection:
    """获取数据库连接（自动建表）"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS seen_items (
            url_hash TEXT PRIMARY KEY,
            title_hash TEXT,
            url TEXT NOT NULL,
            title TEXT,
            source_name TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            total_fetched INTEGER DEFAULT 0,
            after_dedup INTEGER DEFAULT 0,
            after_filter INTEGER DEFAULT 0,
            sources_succeeded INTEGER DEFAULT 0,
            sources_failed INTEGER DEFAULT 0,
            llm_tokens_used INTEGER DEFAULT 0,
            report_path TEXT
        );
    """)
    conn.commit()


def is_seen(url: str) -> bool:
    """检查 URL 是否已处理过"""
    url_hash = _hash(url)
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM seen_items WHERE url_hash = ?", (url_hash,)
    ).fetchone()
    conn.close()
    return row is not None


def mark_seen(url: str, title: str = "", source_name: str = ""):
    """标记 URL 为已处理"""
    url_hash = _hash(url)
    title_hash = _hash(title) if title else ""
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO seen_items (url_hash, title_hash, url, title, source_name, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, COALESCE((SELECT first_seen FROM seen_items WHERE url_hash=?), ?), ?)""",
        (url_hash, title_hash, url, title, source_name, url_hash, now, now),
    )
    conn.commit()
    conn.close()


def log_run(
    total_fetched: int = 0,
    after_dedup: int = 0,
    after_filter: int = 0,
    sources_succeeded: int = 0,
    sources_failed: int = 0,
    llm_tokens_used: int = 0,
    report_path: str = "",
) -> int:
    """记录一次运行日志，返回 log id"""
    conn = get_db()
    now = datetime.now().isoformat()
    cur = conn.execute(
        """INSERT INTO run_log (run_at, total_fetched, after_dedup, after_filter,
           sources_succeeded, sources_failed, llm_tokens_used, report_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, total_fetched, after_dedup, after_filter,
         sources_succeeded, sources_failed, llm_tokens_used, report_path),
    )
    conn.commit()
    log_id = cur.lastrowid
    conn.close()
    return log_id


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()
