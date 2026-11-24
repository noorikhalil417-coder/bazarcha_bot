"""Administrative blocking across the bot, Mini App and configured Telegram group."""

import logging
import sqlite3

from aiohttp import web
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop

from services.marketplace import MarketplaceError
from services.action_locks import action_lock

logger = logging.getLogger(__name__)


async def blocked_user_guard(update, context):
    user = update.effective_user
    chat = update.effective_chat
    config = context.application.bot_data["config"]
    if not user or user.id in config.admin_ids:
        return
    message = update.effective_message
    command = bool(message and (message.text or "").startswith("/"))
    if not (chat and chat.type == "private") and not update.callback_query and not command:
        return
    with context.application.bot_data["database"].connect() as conn:
        blocked = conn.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (user.id,)).fetchone()
    if blocked:
        if update.callback_query:
            await update.callback_query.answer("دسترسی این حساب مسدود شده است.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("دسترسی این حساب مسدود شده است.")
        raise ApplicationHandlerStop


def register_user_routes(app, application):
    db = application.bot_data["marketplace"].db
    config = application.bot_data["config"]
    lock = action_lock(application, "user_block")

    async def users(request):
        page = max(0, min(int(request.query.get("page", "0")), 10000))
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT u.user_id,u.username,u.display_name,u.created_at,u.last_seen_at,"
                "b.reason,b.group_synced FROM users u LEFT JOIN blocked_users b USING(user_id) "
                "ORDER BY u.last_seen_at DESC,u.user_id LIMIT 51 OFFSET ?", (page * 50,)
            ).fetchall()
        return web.json_response({"items": [{**dict(row), "is_admin": row["user_id"] in config.admin_ids}
                                             for row in rows[:50]], "has_more": len(rows) > 50})

    async def set_block(request):
        target = int(request.match_info["user_id"])
        body = await request.json()
        if not isinstance(body, dict) or type(body.get("blocked")) is not bool:
            raise MarketplaceError("درخواست مسدودسازی معتبر نیست.")
        reason = body.get("reason", "")
        if not isinstance(reason, str) or len(reason.strip()) > 500:
            raise MarketplaceError("دلیل مسدودسازی معتبر نیست.")
        if body["blocked"] and not reason.strip():
            raise MarketplaceError("دلیل مسدودسازی لازم است.")
        if target in config.admin_ids:
            raise MarketplaceError("حساب مدیر پیکربندی‌شده قابل مسدودسازی نیست.")
        if config.group_id is None:
            raise MarketplaceError("GROUP_ID تنظیم نشده است؛ مسدودسازی گروه ممکن نیست.")
        admin = request["user"]["id"]
        async with lock:
            with db.connect() as conn:
                exists = conn.execute("SELECT 1 FROM users WHERE user_id=?", (target,)).fetchone()
                old = conn.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (target,)).fetchone()
            if not exists:
                raise web.HTTPNotFound()
            if body["blocked"]:
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO blocked_users(user_id,admin_id,reason,group_synced) VALUES(?,?,?,0) "
                        "ON CONFLICT(user_id) DO UPDATE SET admin_id=excluded.admin_id,reason=excluded.reason,group_synced=0",
                        (target, admin, reason.strip()),
                    )
                    conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'web_block',?)", (admin, str(target)))
                try:
                    await application.bot.ban_chat_member(config.group_id, target)
                except TelegramError as exc:
                    logger.warning("Group ban failed for user %s: %s", target, type(exc).__name__)
                    with db.transaction() as conn:
                        conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'web_group_ban_failed',?)", (admin, str(target)))
                    return web.json_response({"error": "ربات و مینی‌اپ مسدود شدند، اما مسدودسازی گروه ناموفق بود؛ اجازهٔ ربات را بررسی و دوباره تلاش کنید.", "local_blocked": True}, status=502)
                try:
                    with db.transaction() as conn:
                        conn.execute("UPDATE blocked_users SET group_synced=1 WHERE user_id=?", (target,))
                        conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'web_group_ban',?)", (admin, str(target)))
                except sqlite3.Error as exc:
                    logger.error("Group ban succeeded but state recording failed for user %s: %s", target, type(exc).__name__)
                    return web.json_response({"error": "مسدودسازی گروه انجام شد، اما ثبت وضعیت شکست خورد؛ دسترسی‌ها را بررسی کنید.", "local_blocked": True}, status=500)
            else:
                if not old:
                    raise MarketplaceError("این کاربر در پنل وب مسدود نیست.")
                try:
                    await application.bot.unban_chat_member(config.group_id, target, only_if_banned=True)
                except TelegramError as exc:
                    logger.warning("Group unban failed for user %s: %s", target, type(exc).__name__)
                    with db.transaction() as conn:
                        conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'web_group_unban_failed',?)", (admin, str(target)))
                    return web.json_response({"error": "رفع مسدودی گروه انجام نشد؛ حساب در ربات و مینی‌اپ همچنان مسدود است."}, status=502)
                try:
                    with db.transaction() as conn:
                        conn.execute("DELETE FROM blocked_users WHERE user_id=?", (target,))
                        conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'web_unblock',?)", (admin, str(target)))
                except sqlite3.Error as exc:
                    logger.error("Group unban succeeded but local unblock failed for user %s: %s", target, type(exc).__name__)
                    return web.json_response({"error": "محرومیت گروه برداشته شد، اما حساب در ربات و مینی‌اپ همچنان مسدود است؛ دوباره تلاش کنید."}, status=500)
        return web.json_response({"ok": True})

    app.router.add_get("/api/admin/users", users)
    app.router.add_post("/api/admin/users/{user_id}/block", set_block)
