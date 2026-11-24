"""Persistent, per-group invitation attribution and conservative ad detection."""

from __future__ import annotations

import re
import time
from database import Database


def normalize(text: str) -> str:
    return (
        re.sub(
            r"\s+",
            " ",
            text.translate(
                str.maketrans("يكىۀ۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "یکیە01234567890123456789")
            ).replace("\u200c", " "),
        )
        .strip()
        .casefold()
    )


def classify_group_ad(text: str, has_photo: bool = False) -> str:
    """Only explicit commerce with corroborating details is automatically removed."""
    text = normalize(text)
    if len(text) < 10:
        return "normal"
    if re.search(r"(فروشی نیست|به فروش نمی|نمی\s*فروشم|نمی\s*خواهم|نمی\s*خوام)", text):
        return "normal"
    if re.search(r"(چطور|چگونه|راهنما|قوانین|مشکل|دکمه)", text) and re.search(
        r"(آگهی|ربات|مینی\s*اپ)", text
    ):
        return "normal"
    product = bool(
        re.search(
            r"(موبایل|گوشی|سامسونگ|آیفون|لپتاپ|کمپیوتر|موتر|ماشین|بایسکل|خانه|آپارتمان|زمین|لباس|کفش|ساعت|سولر|فرش|یخچال|تلویزیون|برق\s*کار|ترمیم|خدمات|خیاط|نجار|دکان|گهواره|کتاب)",
            text,
        )
    )
    intent = bool(
        re.search(
            r"(فروشی|به فروش|برای فروش|می\s*فروشم|می\s*خواهم\s*بخرم|می\s*خوام\s*بخرم|خریدار.{0,30}هستم|نیاز به خرید|می\s*خواستم|می\s*خاستم|می\s*خواهم|می\s*خوام|برای کرایه|به کرایه|اجاره داده)",
            text,
        )
    )
    price = bool(
        re.search(
            r"(\d[\d٬,، ]*\s*(هزار\s*)?(افغانی|دالر|دلار|تومان|یورو)|قیمت\s*[:：]?\s*\d|توافقی|با جورآمد)",
            text,
        )
    )
    contact = bool(
        re.search(
            r"(تماس بگیرید|پیام بدهید|پیام بگذارید|سفارش|واتساپ|https?://t\.me/|@[a-z0-9_]{5,32}|\+?\d[\d ()-]{7,}\d)",
            text,
        )
    )
    city = bool(
        re.search(r"(کابل|مزار\s*شریف|هرات|قندهار|جلال\s*آباد|کندز|بامیان|غزنی)", text)
    )
    service = bool(
        re.search(
            r"(برق\s*کار|نجار|خیاط|ترمیم|خدمات).{0,50}(هستم|انجام می|ارائه می|آماده)",
            text,
        )
    )
    if (
        intent
        and (product or (price and contact))
        and (price or contact or city or has_photo)
    ):
        return "ad"
    if service and (contact or price or city):
        return "ad"
    if service or product and (price or contact) or intent and product:
        return "review"
    return "normal"


class GroupPolicy:
    def __init__(self, db: Database):
        self.db = db

    def settings(self, chat_id: int) -> dict:
        with self.db.connect() as c:
            row = c.execute(
                "SELECT * FROM group_policies WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return (
            dict(row)
            if row
            else {
                "chat_id": chat_id,
                "invite_required": 0,
                "ad_delete": 0,
                "required_invites": 3,
            }
        )

    def set_enabled(self, chat_id: int, field: str, enabled: bool, admin_id: int):
        if field not in {"invite_required", "ad_delete"}:
            raise ValueError("Invalid setting")
        with self.db.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO group_policies(chat_id) VALUES(?)", (chat_id,)
            )
            c.execute(
                f"UPDATE group_policies SET {field}=? WHERE chat_id=?",
                (int(enabled), chat_id),
            )
            c.execute(
                "INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)",
                (admin_id, "group_policy", f"{chat_id}:{field}:{int(enabled)}"),
            )

    def set_required_invites(self, chat_id: int, count: int, admin_id: int):
        if type(count) is not int or not 1 <= count <= 20:
            raise ValueError("Invalid invite count")
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO group_policies(chat_id) VALUES(?)", (chat_id,))
            conn.execute("UPDATE group_policies SET required_invites=? WHERE chat_id=?", (count, chat_id))
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)",
                         (admin_id, "group_policy", f"{chat_id}:required_invites:{count}"))

    def seen(self, chat_id: int, user_id: int):
        with self.db.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO group_members_seen(chat_id,user_id) VALUES(?,?)",
                (chat_id, user_id),
            )

    def link(self, chat_id: int, owner_id: int):
        with self.db.connect() as c:
            row = c.execute(
                "SELECT invite_link FROM group_invite_links WHERE chat_id=? AND owner_id=?",
                (chat_id, owner_id),
            ).fetchone()
        return row[0] if row else None

    def save_link(self, chat_id: int, owner_id: int, link: str):
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO group_invite_links(chat_id,owner_id,invite_link) VALUES(?,?,?) ON CONFLICT(chat_id,owner_id) DO UPDATE SET invite_link=excluded.invite_link",
                (chat_id, owner_id, link),
            )

    def known_link(self, chat_id: int, link: str) -> bool:
        with self.db.connect() as c:
            return (
                c.execute(
                    "SELECT 1 FROM group_invite_links WHERE chat_id=? AND invite_link=?",
                    (chat_id, link),
                ).fetchone()
                is not None
            )

    def pending_join(self, chat_id: int, user_id: int, link: str):
        if not self.known_link(chat_id, link):
            return
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO group_pending_joins(chat_id,user_id,invite_link,requested_at) VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET invite_link=excluded.invite_link,requested_at=excluded.requested_at",
                (chat_id, user_id, link, int(time.time())),
            )

    def count(self, chat_id: int, owner_id: int) -> int:
        with self.db.connect() as c:
            return c.execute(
                "SELECT count(*) FROM group_referrals WHERE chat_id=? AND inviter_id=?",
                (chat_id, owner_id),
            ).fetchone()[0]

    def register_join(
        self,
        chat_id: int,
        user_id: int,
        link: str | None,
        via_request: bool = False,
        event_at: int | None = None,
    ) -> int | None:
        """Count only the first observed membership; unique group/invitee prevents farming."""
        with self.db.transaction() as c:
            pending = c.execute(
                "SELECT invite_link,requested_at FROM group_pending_joins WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            if (
                not link
                and via_request
                and pending
                and int(time.time()) - pending["requested_at"] < 172800
            ):
                link = pending["invite_link"]
            owner = c.execute(
                "SELECT owner_id,created_at FROM group_invite_links WHERE chat_id=? AND invite_link=?",
                (chat_id, link or ""),
            ).fetchone()
            previous = c.execute(
                "SELECT first_seen_at FROM group_members_seen WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            c.execute(
                "INSERT OR IGNORE INTO group_members_seen(chat_id,user_id,first_seen_at) VALUES(?,?,?)",
                (chat_id, user_id, event_at or int(time.time())),
            )
            c.execute(
                "DELETE FROM group_pending_joins WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            if not owner or owner["owner_id"] == user_id:
                return None
            # A message can race the membership update. Earlier known members never earn a referral;
            # a same-second new-member event is still eligible and can be credited only once.
            if previous and (event_at is None or previous["first_seen_at"] < event_at):
                return None
            if event_at is not None and event_at < owner["created_at"]:
                return None
            cursor = c.execute(
                "INSERT OR IGNORE INTO group_referrals(chat_id,invitee_id,inviter_id) VALUES(?,?,?)",
                (chat_id, user_id, owner["owner_id"]),
            )
            return owner["owner_id"] if cursor.rowcount else None

    def claim_notice(self, chat_id: int, user_id: int, cooldown: int = 90) -> bool:
        now = int(time.time())
        with self.db.transaction() as c:
            cursor = c.execute(
                "INSERT INTO group_gate_notices(chat_id,user_id,last_notice_at) VALUES(?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET last_notice_at=excluded.last_notice_at WHERE group_gate_notices.last_notice_at<=?",
                (chat_id, user_id, now, now - cooldown),
            )
            return cursor.rowcount == 1

    def deleted(self, chat_id: int, message_id: int) -> bool:
        with self.db.connect() as c:
            return (
                c.execute(
                    "SELECT 1 FROM group_gate_actions WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                ).fetchone()
                is not None
            )

    def record_delete(self, chat_id: int, message_id: int, user_id: int, reason: str):
        with self.db.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO group_gate_actions(chat_id,message_id,user_id,reason) VALUES(?,?,?,?)",
                (chat_id, message_id, user_id, reason),
            )

    def prune_logs(self):
        with self.db.transaction() as c:
            c.execute(
                "DELETE FROM group_gate_actions WHERE created_at < strftime('%Y-%m-%dT%H:%M:%fZ','now','-90 days')"
            )
            c.execute(
                "DELETE FROM group_pending_joins WHERE requested_at<?",
                (int(time.time()) - 172800,),
            )
            c.execute(
                "DELETE FROM group_gate_notices WHERE last_notice_at<?",
                (int(time.time()) - 30 * 86400,),
            )
