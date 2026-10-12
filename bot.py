"""Bazaarche Telegram bot entry point."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config
from database import Database
from handlers.admin import _group_message_url, _publish_ad, admin_handlers
from handlers.user import user_handlers
from services.backup import create_backup, prune_backups
from services.marketplace import Marketplace
from services.moderation import Moderation

logger = logging.getLogger(__name__)


def _looks_like_ad(text: str, has_photo: bool) -> bool:
    normalized = text.casefold()
    commerce = bool(re.search(r"(?:فروشی|به فروش|آگهی|کرایه|موجود است|عرضه می‌شود)", normalized))
    price = bool(re.search(r"(?:\d[\d\s,،٬]*\s*(?:افغانی|هزار)|توافقی|با\s*جورآمد)", normalized))
    contact = bool(re.search(r"(?:تماس|شماره|واتساپ|تلگرام|(?:\+?\d[\d ()-]{7,}\d))", normalized))
    if has_photo and len(normalized.strip()) >= 8:
        return commerce or price
    return commerce and (price or contact) or price and contact


async def _notify_group_review(
    context: ContextTypes.DEFAULT_TYPE, event_id: int, chat_id: int, message_id: int,
    chat_title: str, action: str,
) -> None:
    url = _group_message_url(chat_id, message_id)
    buttons = [[InlineKeyboardButton("✅ بررسی شد", callback_data=f"group-review:{event_id}")]]
    if url:
        buttons[0].append(InlineKeyboardButton("بازکردن پیام", url=url))
    text = (
        f"پیام گروه برای بررسی مدیر علامت‌گذاری شد.\n"
        f"گروه: {chat_title}\n"
        f"شناسهٔ بررسی: {event_id}\n"
        f"دلیل: {'احتمال آگهی' if action == 'review_group_ad' else 'پیام مشکوک'}"
    )
    for admin_id in context.application.bot_data["config"].admin_ids:
        try:
            await context.bot.send_message(admin_id, text, reply_markup=InlineKeyboardMarkup(buttons))
        except TelegramError as exc:
            logger.info("Group review notification unavailable for event %s: %s", event_id, type(exc).__name__)


async def _welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    member = update.chat_member
    if not member or member.new_chat_member.status not in {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED}:
        return
    if member.old_chat_member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
        return
    config: Config = context.application.bot_data["config"]
    if config.group_id is not None and member.chat.id != config.group_id:
        return
    person = member.new_chat_member.user
    market: Marketplace = context.application.bot_data["marketplace"]
    if market.get_setting("welcome_enabled", "1") != "1":
        return
    welcome = market.get_setting("welcome_message", config.welcome_message)
    try:
        bot_user = await context.bot.get_me()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 قوانین", callback_data="menu:rules"),
            InlineKeyboardButton("📝 ثبت آگهی", url=f"https://t.me/{bot_user.username}?start=menu"),
        ]])
        await context.bot.send_message(member.chat.id, f"{person.full_name}، {welcome}", reply_markup=keyboard)
    except TelegramError as exc:
        logger.warning("Welcome message failed: %s", type(exc).__name__)


async def _moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    text = message.text or message.caption or ""
    if not text:
        return
    config: Config = context.application.bot_data["config"]
    if config.group_id is not None and chat.id != config.group_id:
        return
    market: Marketplace = context.application.bot_data["marketplace"]
    market.ensure_user(user.id, user.username, user.full_name)
    with context.application.bot_data["database"].connect() as conn:
        row = conn.execute("SELECT trusted FROM users WHERE user_id=?", (user.id,)).fetchone()
        trusted = bool(row and row[0])
    reason = context.application.bot_data["moderation"].check_message(
        user.id, text, trusted=trusted, admin=user.id in config.admin_ids,
        blocked_phrases=tuple(filter(None, market.get_setting("blocked_phrases").split("\n"))),
        links_allowed=market.get_setting("links_allowed", "1") == "1",
    )
    if reason:
        action = "review_" + reason
        event_id, created = market.flag_group_message(user.id, chat.id, message.message_id, chat.username, action)
        if created:
            await _notify_group_review(context, event_id, chat.id, message.message_id, chat.title or "گروه", action)
        try:
            await message.reply_text("پیام شما برای بررسی مدیر علامت‌گذاری شد. اگر اشتباهی رخ داده، با مدیران تماس بگیرید.")
        except TelegramError as exc:
            logger.warning("Moderation notice failed: %s", type(exc).__name__)

    if _looks_like_ad(text, bool(message.photo)):
        event_id, created = market.flag_group_message(user.id, chat.id, message.message_id, chat.username)
        if created:
            await _notify_group_review(context, event_id, chat.id, message.message_id, chat.title or "گروه", "review_group_ad")


async def _handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    request = update.chat_join_request
    if not request:
        return
    config: Config = context.application.bot_data["config"]
    if config.group_id is not None and request.chat.id != config.group_id:
        return
    market: Marketplace = context.application.bot_data["marketplace"]
    market.ensure_user(request.from_user.id, request.from_user.username, request.from_user.full_name)
    if market.get_setting("join_approval", "0") != "1":
        try:
            await context.bot.approve_chat_join_request(request.chat.id, request.from_user.id)
        except TelegramError as exc:
            logger.warning("Join request auto-approval failed: %s", type(exc).__name__)
        return
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ پذیرش", callback_data=f"join:approve:{request.chat.id}:{request.from_user.id}"),
        InlineKeyboardButton("❌ رد", callback_data=f"join:reject:{request.chat.id}:{request.from_user.id}"),
    ]])
    for admin_id in config.admin_ids:
        try:
            await context.bot.send_message(
                admin_id,
                f"درخواست عضویت تازه از {request.from_user.full_name} ({request.from_user.id}) در گروه {request.chat.title}.",
                reply_markup=markup,
            )
        except TelegramError as exc:
            logger.info("Join request notice unavailable: %s", type(exc).__name__)


async def _scheduled_tasks(context: ContextTypes.DEFAULT_TYPE) -> None:
    market: Marketplace = context.application.bot_data["marketplace"]
    database: Database = context.application.bot_data["database"]
    config: Config = context.application.bot_data["config"]
    try:
        expired = await asyncio.to_thread(market.expire_ads)
        if expired:
            logger.info("Expired advertisements: %s", expired)
        rows = await asyncio.to_thread(market.due_expiry_notifications)
        for row in rows:
            if not await asyncio.to_thread(market.mark_expiry_notified, row["ad_id"]):
                continue
            try:
                await context.bot.send_message(row["owner_id"], f"آگهی {row['ad_id']} («{row['title'][:100]}») تا دو روز دیگر پایان می‌یابد. برای تمدید /myads را ببینید.")
            except TelegramError as exc:
                logger.info("Expiry notice unavailable for ad %s: %s", row["ad_id"], type(exc).__name__)
    except (sqlite3.Error, OSError) as exc:
        logger.error("Scheduled task failed: %s", type(exc).__name__)
    if config.automatic_backup_interval_hours:
        claimed = await asyncio.to_thread(market.claim_scheduled_job, "automatic_backup", config.automatic_backup_interval_hours)
        if claimed:
            try:
                path = await asyncio.to_thread(create_backup, database, config.backup_directory)
                await asyncio.to_thread(prune_backups, config.backup_directory, config.backup_retention_days)
                await asyncio.to_thread(market.finish_scheduled_job, "automatic_backup", True)
                logger.info("Scheduled backup created: %s", path.name)
            except (OSError, ValueError, sqlite3.Error) as exc:
                await asyncio.to_thread(market.finish_scheduled_job, "automatic_backup", False)
                logger.error("Scheduled backup failed: %s", type(exc).__name__)
    cleanup_claimed = await asyncio.to_thread(market.claim_scheduled_job, "temporary_cleanup", 24)
    if cleanup_claimed:
        try:
            await asyncio.to_thread(market.cleanup_temporary_state, config.cleanup_retention_days)
            await asyncio.to_thread(prune_backups, config.backup_directory, config.backup_retention_days)
            await asyncio.to_thread(market.finish_scheduled_job, "temporary_cleanup", True)
        except (OSError, ValueError, sqlite3.Error) as exc:
            await asyncio.to_thread(market.finish_scheduled_job, "temporary_cleanup", False)
            logger.error("Scheduled cleanup failed: %s", type(exc).__name__)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    logger.error("Telegram update failed: %s", type(error).__name__ if error else "UnknownError")
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("در انجام درخواست مشکل پیش آمد. دوباره تلاش کنید یا با مدیر تماس بگیرید.")
        except TelegramError as send_error:
            logger.debug("Could not send user-facing error notice: %s", type(send_error).__name__)


async def _post_init(application: Application) -> None:
    if application.job_queue:
        application.job_queue.run_repeating(_scheduled_tasks, interval=3600, first=30, name="marketplace-maintenance")


def build_application(config: Config, database: Database | None = None) -> Application:
    db = database or Database(config.database_path)
    db.initialize()
    application = Application.builder().token(config.token).post_init(_post_init).build()
    application.bot_data["config"] = config
    application.bot_data["database"] = db
    application.bot_data["marketplace"] = Marketplace(db, config.max_active_ads_per_user, config.default_ad_expiry_days)
    application.bot_data["moderation"] = Moderation(db)
    application.bot_data["publish_ad"] = _publish_ad
    for handler in user_handlers() + admin_handlers():
        application.add_handler(handler)
    application.add_handler(ChatMemberHandler(_welcome, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(ChatJoinRequestHandler(_handle_join_request))
    application.add_handler(
        MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.ChatType.GROUPS, _moderate_message),
        group=1,
    )
    application.add_error_handler(_error_handler)
    return application


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(level=getattr(logging, config.log_level), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    application = build_application(config)
    logger.info("Starting Bazaarche long-polling bot")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()