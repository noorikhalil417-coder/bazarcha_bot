"""Durable, coalesced Telegram notifications for private messages."""

import asyncio
import logging
from datetime import timedelta

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

logger = logging.getLogger(__name__)
NOTICE = "پیام تازه‌ای در دوکانچه دارید. برای دیدن آن مینی‌اپ را باز کنید."


def enqueue(conn, user_id, message_id):
    conn.execute(
        "INSERT INTO chat_notification_outbox(user_id,latest_message_id,next_attempt) "
        "VALUES(?,?,coalesce((SELECT max(strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
        "strftime('%Y-%m-%dT%H:%M:%fZ',last_sent_at,'+5 minutes')) FROM chat_notifications WHERE user_id=?),"
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'))) "
        "ON CONFLICT(user_id) DO UPDATE SET latest_message_id=excluded.latest_message_id",
        (user_id, message_id, user_id),
    )


def recover_inflight(db):
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chat_notification_outbox SET state='pending', "
            "next_attempt=strftime('%Y-%m-%dT%H:%M:%fZ','now','+5 minutes'), "
            "last_error='interrupted' WHERE state='inflight'"
        )


class NotificationWorker:
    def __init__(self, db, bot):
        self.db = db
        self.bot = bot
        self.lock = asyncio.Lock()

    async def run(self):
        if self.lock.locked():
            return
        async with self.lock:
            for _ in range(20):
                with self.db.transaction() as conn:
                    if conn.execute(
                        "SELECT not_before>strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                        "FROM chat_notification_control WHERE id=1"
                    ).fetchone()[0]:
                        return
                    row = conn.execute(
                        "SELECT user_id,latest_message_id,attempts FROM chat_notification_outbox "
                        "WHERE state='pending' AND next_attempt<=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                        "ORDER BY next_attempt,user_id LIMIT 1"
                    ).fetchone()
                    if not row:
                        return
                    uid, message_id, attempts = row
                    conn.execute("UPDATE chat_notification_outbox SET state='inflight' WHERE user_id=?", (uid,))
                try:
                    await self.bot.send_message(uid, NOTICE)
                except TelegramError as exc:
                    permanent = isinstance(exc, (Forbidden, BadRequest))
                    delay = min(3600, 30 * 2 ** min(attempts, 7))
                    if isinstance(exc, RetryAfter):
                        value = exc.retry_after
                        delay = max(delay, int(value.total_seconds() if isinstance(value, timedelta) else value) + 1)
                    with self.db.transaction() as conn:
                        conn.execute(
                            "UPDATE chat_notification_outbox SET state=?,attempts=attempts+1,"
                            "next_attempt=strftime('%Y-%m-%dT%H:%M:%fZ','now',?),last_error=? WHERE user_id=?",
                            ('failed' if permanent else 'pending', f'+{delay} seconds', type(exc).__name__, uid),
                        )
                        if isinstance(exc, RetryAfter):
                            conn.execute(
                                "UPDATE chat_notification_control SET not_before=max(not_before,"
                                "strftime('%Y-%m-%dT%H:%M:%fZ','now',?)) WHERE id=1", (f'+{delay} seconds',),
                            )
                    logger.warning("Chat notification failed for user %s: %s", uid, type(exc).__name__)
                    if isinstance(exc, RetryAfter):
                        return
                except Exception:
                    with self.db.transaction() as conn:
                        conn.execute(
                            "UPDATE chat_notification_outbox SET state='pending',attempts=attempts+1,"
                            "next_attempt=strftime('%Y-%m-%dT%H:%M:%fZ','now','+5 minutes'),"
                            "last_error='unexpected' WHERE user_id=?", (uid,),
                        )
                    logger.exception("Unexpected chat notification error for user %s", uid)
                    raise
                else:
                    with self.db.transaction() as conn:
                        conn.execute(
                            "INSERT INTO chat_notifications(user_id,last_sent_at) VALUES(?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                            "ON CONFLICT(user_id) DO UPDATE SET last_sent_at=excluded.last_sent_at", (uid,),
                        )
                        conn.execute(
                            "DELETE FROM chat_notification_outbox WHERE user_id=? AND latest_message_id=?",
                            (uid, message_id),
                        )
                        conn.execute(
                            "UPDATE chat_notification_outbox SET state='pending',attempts=0,last_error=NULL,"
                            "next_attempt=strftime('%Y-%m-%dT%H:%M:%fZ','now','+5 minutes') WHERE user_id=?",
                            (uid,),
                        )


async def notification_job(context):
    await context.application.bot_data["chat_notification_worker"].run()
