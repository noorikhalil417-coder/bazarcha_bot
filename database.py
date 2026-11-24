"""SQLite persistence and schema migrations."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 13
DEFAULT_CATEGORIES = (
    "موبایل و تکنالوژی", "موتر و وسایط", "خانه و جایداد", "لباس و لوازم شخصی",
    "لوازم خانه", "خدمات", "کار و استخدام", "حیوانات و محصولات زراعتی", "سایر موارد",
)
AFGHAN_PROVINCES = (
    "ارزگان", "بادغیس", "بامیان", "بدخشان", "بغلان", "بلخ", "پروان", "پکتیا",
    "پکتیکا", "پنجشیر", "تخار", "جوزجان", "دایکندی", "زابل", "سرپل", "سمنگان",
    "غزنی", "غور", "فاریاب", "فراه", "کابل", "کاپیسا", "کندز", "کندهار",
    "کنر", "خوست", "لغمان", "لوگر", "میدان وردک", "ننگرهار", "نورستان",
    "نیمروز", "هرات", "هلمند",
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
                    connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('welcome_message','به دوکانچه خوش آمدید! برای ثبت یا جست‌وجوی آگهی از منوی ربات استفاده کنید.')")
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
                    version = 3
                if version < 4:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("ALTER TABLE ads ADD COLUMN miniapp_revision INTEGER NOT NULL DEFAULT 0")
                    connection.execute("CREATE TABLE miniapp_media (file_id TEXT PRIMARY KEY, ad_id INTEGER NOT NULL REFERENCES ads(ad_id) ON DELETE CASCADE, data BLOB NOT NULL CHECK(length(data)<=2097152))")
                    connection.execute("PRAGMA user_version = 4")
                    connection.commit()
                    version = 4
                if version < 5:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("CREATE TABLE IF NOT EXISTS miniapp_favorites (user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE, ad_id INTEGER NOT NULL REFERENCES ads(ad_id) ON DELETE CASCADE, PRIMARY KEY(user_id,ad_id))")
                    connection.execute("CREATE TABLE IF NOT EXISTS miniapp_profiles (user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE, city TEXT NOT NULL DEFAULT '', notifications_read_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z')")
                    connection.execute("PRAGMA user_version = 5")
                    connection.commit()
                    version = 5
                if version < 6:
                    from content import DEFAULT_RULES
                    connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_V6)
                    connection.execute("UPDATE settings SET value=? WHERE key='rules' AND value=?", (DEFAULT_RULES, 'با احترام رفتار کنید. آگهی نادرست، تکراری و محتوای خلاف قانون مجاز نیست.'))
                    connection.execute("PRAGMA user_version = 6")
                    connection.commit()
                    version = 6
                if version < 7:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS private_panels (chat_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, message_id INTEGER NOT NULL, callbacks TEXT NOT NULL)"
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS private_panel_messages (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id))"
                    )
                    connection.execute("PRAGMA user_version = 7")
                    connection.commit()
                    version = 7
                if version < 8:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS marketplace_metrics "
                        "(id INTEGER PRIMARY KEY CHECK(id=1), ad_views INTEGER NOT NULL DEFAULT 0, "
                        "started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
                    )
                    connection.execute("INSERT OR IGNORE INTO marketplace_metrics(id) VALUES(1)")
                    connection.execute("PRAGMA user_version = 8")
                    connection.commit()
                    version = 8
                if version < 9:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS banners ("
                        "banner_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "placement TEXT NOT NULL CHECK(placement IN ('home','browse')), "
                        "title TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', "
                        "image BLOB NOT NULL CHECK(length(image)<=2097152), "
                        "starts_at INTEGER, ends_at INTEGER, "
                        "enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)), "
                        "sort_order INTEGER NOT NULL DEFAULT 0, "
                        "CHECK(starts_at IS NULL OR ends_at IS NULL OR starts_at<ends_at))"
                    )
                    connection.execute("PRAGMA user_version = 9")
                    connection.commit()
                    version = 9
                if version < 10:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS blocked_users ("
                        "user_id INTEGER PRIMARY KEY REFERENCES users(user_id), "
                        "admin_id INTEGER NOT NULL, reason TEXT NOT NULL, "
                        "group_synced INTEGER NOT NULL DEFAULT 0 CHECK(group_synced IN (0,1)), "
                        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
                    )
                    connection.execute("PRAGMA user_version = 10")
                    connection.commit()
                    version = 10
                if version < 11:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS provinces ("
                        "province_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
                        "enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), sort_order INTEGER NOT NULL DEFAULT 0)"
                    )
                    connection.executemany("INSERT OR IGNORE INTO provinces(name,sort_order) VALUES(?,?)",
                                           [(name, index) for index, name in enumerate(AFGHAN_PROVINCES)])
                    connection.execute("PRAGMA user_version = 11")
                    connection.commit()
                    version = 11
                if version < 12:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS ad_publication_history ("
                        "ad_id INTEGER NOT NULL REFERENCES ads(ad_id), chat_id INTEGER NOT NULL, "
                        "message_id INTEGER NOT NULL, archived_at TEXT NOT NULL DEFAULT "
                        "(strftime('%Y-%m-%dT%H:%M:%fZ','now')), PRIMARY KEY(ad_id,chat_id,message_id))"
                    )
                    connection.execute("PRAGMA user_version = 12")
                    connection.commit()
                    version = 12
                if version < 13:
                    connection.executescript("""
                        BEGIN IMMEDIATE;
                        CREATE TABLE IF NOT EXISTS conversations (
                            conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            ad_id INTEGER NOT NULL REFERENCES ads(ad_id),
                            buyer_id INTEGER NOT NULL REFERENCES users(user_id),
                            seller_id INTEGER NOT NULL REFERENCES users(user_id),
                            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                            CHECK(buyer_id != seller_id),
                            UNIQUE(ad_id,buyer_id,seller_id)
                        );
                        CREATE TABLE IF NOT EXISTS chat_messages (
                            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id),
                            sender_id INTEGER NOT NULL REFERENCES users(user_id),
                            client_id TEXT NOT NULL,
                            body TEXT NOT NULL DEFAULT '',
                            image BLOB CHECK(image IS NULL OR length(image)<=2097152),
                            fingerprint TEXT NOT NULL,
                            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                            CHECK(length(body)>0 OR image IS NOT NULL),
                            UNIQUE(conversation_id,sender_id,client_id)
                        );
                        CREATE INDEX IF NOT EXISTS chat_messages_conversation ON chat_messages(conversation_id,message_id);
                        CREATE INDEX IF NOT EXISTS chat_messages_sender_time ON chat_messages(sender_id,created_at);
                        CREATE TABLE IF NOT EXISTS chat_reads (
                            conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id),
                            user_id INTEGER NOT NULL REFERENCES users(user_id),
                            last_message_id INTEGER NOT NULL DEFAULT 0,
                            PRIMARY KEY(conversation_id,user_id)
                        );
                        CREATE TABLE IF NOT EXISTS chat_blocks (
                            blocker_id INTEGER NOT NULL REFERENCES users(user_id),
                            blocked_id INTEGER NOT NULL REFERENCES users(user_id),
                            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                            PRIMARY KEY(blocker_id,blocked_id),
                            CHECK(blocker_id != blocked_id)
                        );
                        CREATE TABLE IF NOT EXISTS chat_reports (
                            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id),
                            message_id INTEGER NOT NULL REFERENCES chat_messages(message_id),
                            reporter_id INTEGER NOT NULL REFERENCES users(user_id),
                            reason TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','reviewed')),
                            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                            UNIQUE(message_id,reporter_id)
                        );
                        CREATE TABLE IF NOT EXISTS chat_notifications (
                            user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                            last_sent_at TEXT NOT NULL
                        );
                        PRAGMA user_version = 13;
                        COMMIT;
                    """)
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


_SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS group_policies (
 chat_id INTEGER PRIMARY KEY,
 invite_required INTEGER NOT NULL DEFAULT 0 CHECK(invite_required IN (0,1)),
 ad_delete INTEGER NOT NULL DEFAULT 0 CHECK(ad_delete IN (0,1)),
 required_invites INTEGER NOT NULL DEFAULT 3 CHECK(required_invites BETWEEN 1 AND 20)
);
CREATE TABLE IF NOT EXISTS group_invite_links (
 chat_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, invite_link TEXT NOT NULL UNIQUE,
 created_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(chat_id,owner_id)
);
CREATE TABLE IF NOT EXISTS group_members_seen (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 first_seen_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS group_referrals (
 chat_id INTEGER NOT NULL, invitee_id INTEGER NOT NULL, inviter_id INTEGER NOT NULL,
 created_at INTEGER NOT NULL DEFAULT (unixepoch()),
 PRIMARY KEY(chat_id,invitee_id), CHECK(invitee_id!=inviter_id)
);
CREATE INDEX IF NOT EXISTS group_referrals_owner ON group_referrals(chat_id,inviter_id);
CREATE TABLE IF NOT EXISTS group_pending_joins (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, invite_link TEXT NOT NULL,
 requested_at INTEGER NOT NULL, PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS group_gate_notices (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, last_notice_at INTEGER NOT NULL,
 PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS group_gate_actions (
 chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(chat_id,message_id)
);
"""
