"""Deterministic moderation helpers and warning escalation."""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque

from database import Database


class Moderation:
    def __init__(self, database: Database, max_messages: int = 8, window_seconds: int = 10, repeat_limit: int = 3):
        self.db = database
        self.max_messages = max_messages
        self.window_seconds = window_seconds
        self.repeat_limit = repeat_limit
        self._recent: dict[int, deque[tuple[float, str]]] = defaultdict(deque)

    def check_message(self, user_id: int, text: str, trusted: bool = False, admin: bool = False,
                      blocked_phrases: tuple[str, ...] = (), links_allowed: bool = True) -> str | None:
        if trusted or admin:
            return None
        now = time.monotonic()
        recent = self._recent[user_id]
        while recent and now - recent[0][0] > self.window_seconds:
            recent.popleft()
        normalized = re.sub(r"\s+", " ", text).strip().casefold()
        repeated = sum(message == normalized for _, message in recent)
        recent.append((now, normalized))
        if len(recent) > self.max_messages:
            recent.popleft()
            return "frequency"
        if normalized and repeated >= self.repeat_limit:
            return "repeated"
        if any(phrase.casefold() in normalized for phrase in blocked_phrases if phrase.strip()):
            return "blocked_phrase"
        if not links_allowed and re.search(r"(?:https?://|t\.me/|www\.)\S+", text, re.IGNORECASE):
            return "link"
        return None

    def warn(self, user_id: int, admin_id: int, reason: str) -> int:
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (admin_id,))
            conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
            conn.execute("INSERT INTO warnings(user_id,admin_id,reason) VALUES(?,?,?)", (user_id, admin_id, reason[:500]))
            conn.execute("INSERT INTO moderation_events(user_id,admin_id,action,reason) VALUES(?,?,'warn',?)", (user_id, admin_id, reason[:500]))
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'warn',?)", (admin_id, str(user_id)))
            return int(conn.execute("SELECT count(*) FROM warnings WHERE user_id=?", (user_id,)).fetchone()[0])

    def warning_count(self, user_id: int) -> int:
        with self.db.connect() as conn:
            return int(conn.execute("SELECT count(*) FROM warnings WHERE user_id=?", (user_id,)).fetchone()[0])

    def record_action(self, user_id: int, admin_id: int, action: str, reason: str = "") -> None:
        if action not in {"mute", "unmute", "ban", "unban"}:
            raise ValueError("Unsupported moderation action")
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
            conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (admin_id,))
            conn.execute("INSERT INTO moderation_events(user_id,admin_id,action,reason) VALUES(?,?,?,?)", (user_id, admin_id, action, reason[:500]))
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)", (admin_id, action, str(user_id)))