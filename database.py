"""SQLite persistence and schema migrations."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3
DEFAULT_CATEGORIES = (
    "موبایل و تکنالوژی", "موتر و وسایط", "خانه و جایداد", "لباس و لوازم شخصی",
    "لوازم خانه", "خدمات", "کار و استخدام", "حیوانات و محصولات زراعتی", "سایر موارد",
)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        self._lock.acquire()
        try:
            connection = sqlite3.connect(self.path, timeout=15, isolation_level=None, factory=_ClosingConnection)
            connection._release_database_lock = self._lock.release
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 15000")
            return connection
        except Exception:
            self._lock.release()
            raise

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._lock:
            yield

    def initialize(self) -> None:
        with self._lock:
            connection = self.connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise RuntimeError("نسخهٔ پایگاه‌داده از نسخهٔ برنامه پیشرفته‌تر است.")
                if version < 1:
                    connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_V1)
                    connection.executemany(
                        "INSERT INTO categories(name, sort_order) VALUES (?, ?)",
                        [(name, index) for index, name in enumerate(DEFAULT_CATEGORIES)],
                    )
                    connection.execute("PRAGMA user_version = 1")
                    connection.commit()
                    version = 1
                if version < 2:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("ALTER TABLE ads ADD COLUMN expiry_notified_at TEXT")
                    connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('rules','با احترام رفتار کنید. آگهی نادرست، تکراری و محتوای خلاف قانون مجاز نیست.')")
                    connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('welcome_message','به بازارچه خوش آمدید! برای ثبت آگهی /newad را بزنید.')")
                    connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('blocked_phrases','')")
                    connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('links_allowed','1')")
                    connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('review_mode','required')")
                    connection.execute("PRAGMA user_version = 2")
                    connection.commit()
                    version = 2
                if version < 3:
                    connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_V3)
                    connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('public_rankings','1')")
                    connection.execute("PRAGMA user_version = 3")
                    connection.commit()
            finally:
                connection.close()

    def checkpoint(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(FULL)")


class _ClosingConnection(sqlite3.Connection):
    def close(self):
        release = getattr(self, "_release_database_lock", None)
        if release is not None:
            self._release_database_lock = None
            try:
                return super().close()
            finally:
                release()
        return super().close()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


_SCHEMA_V1 = """
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT NOT NULL DEFAULT '',
    trusted INTEGER NOT NULL DEFAULT 0 CHECK(trusted IN (0,1)),
    reputation INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE categories (
    category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
);
CREATE TABLE ads (
    ad_id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(user_id),
    category_id INTEGER NOT NULL REFERENCES categories(category_id),
    ad_type TEXT NOT NULL CHECK(ad_type IN ('sale','wanted','rental','service')),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    price_minor INTEGER,
    currency TEXT NOT NULL DEFAULT 'AFN',
    price_negotiable INTEGER NOT NULL DEFAULT 0 CHECK(price_negotiable IN (0,1)),
    city TEXT NOT NULL,
    contact TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','pending','approved','publishing','publication_failed','published','rejected','expired','archived','sold')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at TEXT,
    publication_chat_id INTEGER,
    publication_message_id INTEGER,
    published_at TEXT,
    featured_until TEXT,
    review_note TEXT,
    publish_claimed_at TEXT
);
CREATE TABLE ad_photos (
    ad_id INTEGER NOT NULL REFERENCES ads(ad_id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(ad_id, file_id)
);
CREATE TABLE warnings (
    warning_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    admin_id INTEGER NOT NULL REFERENCES users(user_id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE moderation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(user_id),
    admin_id INTEGER REFERENCES users(user_id),
    ad_id INTEGER REFERENCES ads(ad_id),
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id INTEGER NOT NULL REFERENCES ads(ad_id),
    reporter_id INTEGER NOT NULL REFERENCES users(user_id),
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','reviewed','dismissed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(ad_id, reporter_id)
);
CREATE TABLE reputation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    ad_id INTEGER REFERENCES ads(ad_id),
    points INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE admin_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE processed_updates (update_id INTEGER PRIMARY KEY, processed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE INDEX ads_owner_status ON ads(owner_id, status);
CREATE INDEX ads_browse ON ads(status, category_id, ad_type, city);
CREATE INDEX ads_expiry ON ads(status, expires_at);
CREATE INDEX reports_status ON reports(status, report_id);
CREATE INDEX warnings_user ON warnings(user_id, created_at);
CREATE INDEX moderation_user ON moderation_events(user_id, created_at);
CREATE INDEX reputation_user ON reputation_events(user_id, created_at);
"""

_SCHEMA_V3 = """
CREATE TABLE search_sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    keyword TEXT NOT NULL DEFAULT '',
    category_id INTEGER REFERENCES categories(category_id),
    city TEXT NOT NULL DEFAULT '',
    ad_type TEXT CHECK(ad_type IN ('sale','wanted','rental','service') OR ad_type IS NULL),
    min_price INTEGER,
    max_price INTEGER,
    pending_field TEXT CHECK(pending_field IN ('keyword','city','price') OR pending_field IS NULL),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX search_sessions_created ON search_sessions(created_at);
CREATE TABLE scheduled_state (
    job_key TEXT PRIMARY KEY,
    last_run_at TEXT,
    lease_until TEXT
);
CREATE TABLE ad_publication_messages (
    ad_id INTEGER NOT NULL REFERENCES ads(ad_id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(ad_id, message_id)
);
"""