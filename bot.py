"""Bazaarche Telegram bot entry point."""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config
from database import Database
from handlers.admin import _publish_ad, admin_handlers
from handlers.user import user_handlers
from services.backup import create_backup, prune_backups
from services.marketplace import Marketplace
from services.moderation import Moderation

logger = logging.getLogger(__name__)


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
    welcome = context.application.bot_data["marketplace"].get_setting("welcome_message", config.welcome_message)
    try:
        await context.bot.send_message(member.chat.id, f"{person.full_name}، {welcome}")
    except TelegramError as exc:
        logger.warning("Welcome message failed: %s", type(exc).__name__)


async def _moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return
    config: Config = context.application.bot_data["config"]
    if config.group_id is not None and update.effective_chat.id != config.group_id:
        return
    market: Marketplace = context.application.bot_data["marketplace"]
    market.ensure_user(user.id, user.username, user.full_name)
    with context.application.bot_data["database"].connect() as conn:
        row = conn.execute("SELECT trusted FROM users WHERE user_id=?", (user.id,)).fetchone()
        trusted = bool(row and row[0])
    reason = context.application.bot_data["moderation"].check_message(
        user.id, message.text, trusted=trusted, admin=user.id in config.admin_ids,
        blocked_phrases=tuple(filter(None, market.get_setting("blocked_phrases").split("\n"))),
        links_allowed=market.get_setting("links_allowed", "1") == "1",
    )
    if not reason:
        return
    with context.application.bot_data["database"].transaction() as conn:
        conn.execute("INSERT INTO moderation_events(user_id,action,reason) VALUES(?,?,?)", (user.id, "review_" + reason, "پیام برای بررسی مدیر علامت‌گذاری شد"))
    try:
        await message.reply_text("پیام شما برای بررسی مدیر علامت‌گذاری شد. اگر اشتباهی رخ داده، با مدیران تماس بگیرید.")
    except TelegramError as exc:
        logger.warning("Moderation notice failed: %s", type(exc).__name__)


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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, _moderate_message), group=1)
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