"""Administrator commands, guarded callbacks, review, and dashboard."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from services.backup import create_backup, restore_backup
from services.marketplace import Marketplace, MarketplaceError, format_public_ad

logger = logging.getLogger(__name__)

SECTIONS = (
    ("stats", "آمار عمومی"), ("ads", "بررسی آگهی‌ها"), ("users", "مدیریت کاربران"),
    ("reports", "گزارش تخلفات"), ("categories", "دسته‌بندی‌ها"), ("rules", "قوانین گروه"),
    ("antispam", "تنظیمات ضداسپم"), ("publication", "تنظیمات نشر آگهی"),
    ("group", "تنظیمات گروه"), ("group_review", "پیام‌های مشکوک"),
    ("history", "سابقهٔ مدیریت"), ("featured", "آگهی‌های ویژه"), ("reputation", "امتیاز کاربران"),
    ("backup", "پشتیبان‌گیری"), ("health", "وضعیت ربات"), ("guide", "راهنمای مدیر"),
)


def _is_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    return user_id in context.application.bot_data["config"].admin_ids


def _market(context: ContextTypes.DEFAULT_TYPE) -> Marketplace:
    return context.application.bot_data["marketplace"]


async def _deny(query) -> None:
    await query.answer("دسترسی به این بخش ندارید.", show_alert=True)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_admin(context, user.id):
        await update.effective_message.reply_text("شما اجازهٔ دسترسی به پنل مدیر را ندارید.")
        return
    await update.effective_message.reply_text("پنل مدیریت بازارچه:", reply_markup=_dashboard())


def _dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"admin:{key}") for key, label in SECTIONS[index:index + 2]] for index in range(0, len(SECTIONS), 2)])


def _group_message_url(chat_id: int, message_id: int, username: str | None = None) -> str | None:
    if username and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return f"https://t.me/{username}/{message_id}"
    raw_chat_id = str(chat_id)
    if raw_chat_id.startswith("-100"):
        return f"https://t.me/c/{raw_chat_id[4:]}/{message_id}"
    return None


async def notify_ad_reviewers(context: ContextTypes.DEFAULT_TYPE, ad_id: int) -> None:
    market: Marketplace = _market(context)
    ad = market.get_ad(ad_id)
    if not ad:
        logger.error("Cannot notify administrators about missing ad %s", ad_id)
        return
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأیید و نشر", callback_data=f"ad:approve:{ad_id}")],
        [InlineKeyboardButton("📝 درخواست تکمیل اطلاعات", callback_data=f"ad:request:{ad_id}")],
        [InlineKeyboardButton("❌ رد آگهی", callback_data=f"ad:reject:{ad_id}")],
    ])
    text = f"آگهی شمارهٔ {ad_id} در انتظار بررسی مدیر است.\n\n{format_public_ad(ad)}"
    photos = market.photos(ad_id)
    for admin_id in context.application.bot_data["config"].admin_ids:
        try:
            if photos:
                try:
                    if len(photos) == 1:
                        await context.bot.send_photo(admin_id, photos[0], caption=f"پیش‌نمایش آگهی #{ad_id}")
                    else:
                        media = [InputMediaPhoto(media=file_id, caption=f"پیش‌نمایش آگهی #{ad_id}" if index == 0 else None) for index, file_id in enumerate(photos)]
                        await context.bot.send_media_group(admin_id, media)
                except TelegramError as exc:
                    logger.info("Review photo preview failed for ad %s: %s", ad_id, type(exc).__name__)
            await context.bot.send_message(admin_id, text, reply_markup=markup)
        except TelegramError as exc:
            logger.info("Administrator review notification unavailable for ad %s: %s", ad_id, type(exc).__name__)


async def _dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(context, query.from_user.id):
        await _deny(query)
        return
    await query.answer()
    section = query.data.partition(":")[2]
    if section == "home":
        await query.edit_message_text("پنل مدیریت بازارچه:", reply_markup=_dashboard())
        return
    known = dict(SECTIONS)
    if section not in known:
        await query.edit_message_text("این بخش پیدا نشد.")
        return
    db = context.application.bot_data["database"]
    text, rows = known[section], []
    if section == "stats":
        with db.connect() as conn:
            counts = {row[0]: row[1] for row in conn.execute("SELECT status,count(*) FROM ads GROUP BY status")}
            users = conn.execute("SELECT count(*) FROM users").fetchone()[0]
            reports = conn.execute("SELECT count(*) FROM reports WHERE status='open'").fetchone()[0]
        text += f"\nکاربران ثبت‌شده: {users}\nگزارش باز: {reports}\n" + "\n".join(f"آگهی {key}: {value}" for key, value in counts.items())
    elif section == "ads":
        rows = _market(context).pending_ads()
        text += "\nآگهی‌های در انتظار بررسی:"
        for ad in rows:
            text += f"\n#{ad['ad_id']} | {html.escape(ad['title'])} | {html.escape(ad['category_name'])}"
        if not rows: text += "\nآگهی در انتظار نیست."
    elif section == "reports":
        rows = _market(context).open_reports()
        text += "\nگزارش‌های باز:"
        for report in rows:
            text += f"\nگزارش {report['report_id']}، آگهی {report['ad_id']}: {html.escape(report['title'])} ({html.escape(report['category'])})"
        if not rows: text += "\nگزارش بازی نیست."
    elif section == "group":
        config = context.application.bot_data["config"]
        welcome = _market(context).get_setting("welcome_message", config.welcome_message)
        text += (
            f"\nخوش‌آمدگویی: {'روشن' if _market(context).get_setting('welcome_enabled', '1') == '1' else 'خاموش'}"
            f"\nمتن فعلی: {html.escape(welcome)}"
            "\nبرای تغییر متن از /welcome_set متن تازه استفاده کنید."
            f"\nبررسی درخواست عضویت: {'با تأیید مدیر' if _market(context).get_setting('join_approval', '0') == '1' else 'خودکار'}"
            "\nبرای صف درخواست، گروه باید لینک عضویتِ نیازمند تأیید داشته باشد و ربات اجازهٔ مدیریت پیوندها را بگیرد."
        )
    elif section == "group_review":
        rows = _market(context).pending_group_reviews()
        text += "\nپیام‌هایی که به‌صورت احتمالی شبیه آگهی‌اند؛ پیام اصلی دست‌نخورده می‌ماند:"
        for row in rows:
            try:
                chat_id, message_id, *_ = row["reason"].split(":")
                description = "احتمال آگهی" if row["action"] == "review_group_ad" else "پیام مشکوک"
                text += f"\n{description} #{row['event_id']} | کاربر {row['user_id']} | پیام {message_id} در گروه {chat_id}"
            except (ValueError, TypeError):
                text += f"\nپیام #{row['event_id']} | پیوند پیام نامعتبر است."
        if not rows:
            text += "\nموردی در صف بررسی نیست."
    elif section == "history":
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT admin_id,action,target,created_at FROM admin_actions ORDER BY action_id DESC LIMIT 15"
            ).fetchall()
        action_labels = {
            "ad_approved": "تأیید آگهی",
            "ad_rejected": "رد آگهی یا درخواست اصلاح",
            "ad_archived": "بایگانی آگهی",
            "group_message_review": "بررسی پیام گروه",
            "join_request_approve": "پذیرش درخواست عضویت",
            "join_request_reject": "رد درخواست عضویت",
            "warn": "ثبت اخطار",
            "mute": "خاموش‌کردن کاربر",
            "unmute": "رفع خاموشی کاربر",
            "ban": "مسدودکردن کاربر",
            "unban": "رفع مسدودی کاربر",
        }
        text += "\n۱۵ اقدام مدیریتی تازه:"
        for row in rows:
            text += f"\n{html.escape(row['created_at'])} | مدیر {row['admin_id']} | {action_labels.get(row['action'], 'به‌روزرسانی تنظیم یا آگهی')} | {html.escape(row['target'])}"
        if not rows:
            text += "\nهنوز اقدامی ثبت نشده است."
    elif section == "users":
        with db.connect() as conn:
            rows = conn.execute("SELECT user_id,display_name,username,trusted FROM users ORDER BY last_seen_at DESC LIMIT 10").fetchall()
        text += "\nکاربران تازه‌فعال؛ دکمه برای تغییر وضعیت کاربر:"
        for row in rows:
            text += f"\n{row['user_id']} | {html.escape(row['display_name'] or row['username'] or 'کاربر')} | {'مورد اعتماد' if row['trusted'] else 'عادی'}"
    elif section == "categories":
        text += "\nدکمه برای فعال/غیرفعال‌سازی. فرمان‌ها: /category_add نام، /category_rename شناسه نام، /category_move شناسه جایگاه"
        rows = _market(context).categories(False)
        for row in rows:
            text += f"\n{'فعال' if row['enabled'] else 'غیرفعال'} | {html.escape(row['name'])}"
    elif section == "rules":
        text += "\n" + html.escape(_market(context).get_setting("rules", context.application.bot_data["config"].group_rules)) + "\nتغییر: /rules_set متن تازه"
    elif section == "antispam":
        text += f"\nعبارت‌های بسته: {html.escape(_market(context).get_setting('blocked_phrases', ''))}\nپیوندها: {'مجاز' if _market(context).get_setting('links_allowed', '1') == '1' else 'ممنوع'}\nبرای تغییر از دکمه استفاده کنید."
    elif section == "publication":
        config = context.application.bot_data["config"]
        text += f"\nتأیید مدیر پیش از نشر: اجباری\nمقصد نشر تنظیم‌شده: {'بلی' if config.publication_chat_id else 'نخیر؛ AD_PUBLICATION_CHAT_ID را تنظیم کنید'}\nآگهی‌های ویژه با فرمان ویژه فعال می‌شوند."
    elif section == "featured":
        with db.connect() as conn:
            rows = conn.execute("SELECT ad_id,title,CASE WHEN featured_until > strftime('%Y-%m-%dT%H:%M:%fZ','now') THEN 1 ELSE 0 END AS featured FROM ads WHERE status='published' ORDER BY ad_id DESC LIMIT 10").fetchall()
        text += "\nبرای ویژه/عادی‌کردن آگهی دکمه را بزنید."
        for row in rows:
            text += f"\n#{row['ad_id']} {'ویژه' if row['featured'] else 'عادی'} | {html.escape(row['title'])}"
    elif section == "reputation":
        with db.connect() as conn:
            rows = conn.execute("SELECT user_id,display_name,reputation FROM users ORDER BY reputation DESC,user_id LIMIT 10").fetchall()
        text += "\nامتیاز فعالیت (گواه اعتماد نیست):"
        for row in rows: text += f"\n{row['user_id']} | {html.escape(row['display_name'] or 'کاربر')} | {row['reputation']}"
        text += f"\nرتبه‌بندی همگانی: {'روشن' if _market(context).get_setting('public_rankings', '1') == '1' else 'خاموش'}"
    elif section == "backup":
        text += "\nپشتیبان‌گیری آنلاین از دیتابیس فعلی. بازیابی: /restore_db نام_فایل تأیید. پیش از بازیابی پشتیبان جدا ساخته می‌شود."
    elif section == "health":
        with db.connect() as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        with db.connect() as conn:
            schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        text += f"\nSQLite: {html.escape(integrity)}\nنسخهٔ طرح: {schema_version}\nپردازش: فعال"
    elif section == "guide":
        text += "\nبررسی آگهی‌ها را با تأیید یا رد انجام دهید. برای فرمان‌ها /help را ببینید. شناسهٔ کاربر را از تلگرام بگیرید؛ نام کاربری مدرک هویت نیست. اطلاعات تماس را در گزارش عمومی ننویسید."

    buttons = []
    if section == "ads":
        buttons = [[InlineKeyboardButton("تأیید و نشر", callback_data=f"ad:approve:{r['ad_id']}"), InlineKeyboardButton("درخواست اصلاح", callback_data=f"ad:edits:{r['ad_id']}"), InlineKeyboardButton("رد", callback_data=f"ad:reject:{r['ad_id']}"), InlineKeyboardButton("بایگانی", callback_data=f"ad:archive:{r['ad_id']}")] for r in rows]
    elif section == "reports":
        buttons = [[InlineKeyboardButton(f"بررسی {r['report_id']}", callback_data=f"report-review:{r['report_id']}"), InlineKeyboardButton("رد گزارش", callback_data=f"report-dismiss:{r['report_id']}")] for r in rows]
    elif section == "users":
        buttons = [[InlineKeyboardButton(f"تغییر اعتماد {r['user_id']}", callback_data=f"trust:{r['user_id']}")] for r in rows]
    elif section == "categories":
        buttons = [[InlineKeyboardButton(f"{'غیرفعال‌کردن' if r['enabled'] else 'فعال‌کردن'}: {r['name']}", callback_data=f"category:{r['category_id']}")] for r in rows]
    elif section == "antispam":
        buttons = [[InlineKeyboardButton("تغییر اجازهٔ پیوند", callback_data="setting:links_allowed"), InlineKeyboardButton("افزودن عبارت بسته", callback_data="setting:blocked_phrases")]]
    elif section == "publication":
        buttons = []
    elif section == "group":
        buttons = [[
            InlineKeyboardButton("روشن/خاموش خوش‌آمدگویی", callback_data="setting:welcome_enabled"),
            InlineKeyboardButton("تغییر تأیید عضویت", callback_data="setting:join_approval"),
        ]]
    elif section == "group_review":
        buttons = []
        for row in rows:
            try:
                chat_id_text, message_id_text, *username_parts = row["reason"].split(":")
                chat_id, message_id = int(chat_id_text), int(message_id_text)
                username = username_parts[0] if username_parts else None
                row_buttons = [InlineKeyboardButton(f"✅ بررسی شد #{row['event_id']}", callback_data=f"group-review:{row['event_id']}")]
                url = _group_message_url(chat_id, message_id, username)
                if url:
                    row_buttons.append(InlineKeyboardButton("بازکردن پیام", url=url))
                buttons.append(row_buttons)
            except (ValueError, TypeError):
                continue
    elif section == "reputation":
        buttons = [[InlineKeyboardButton("تغییر نشر رتبه‌بندی", callback_data="setting:public_rankings")]]
    elif section == "featured":
        buttons = [[InlineKeyboardButton(f"تغییر آگهی {r['ad_id']}", callback_data=f"featured:{r['ad_id']}")] for r in rows]
    elif section == "backup":
        buttons = [[InlineKeyboardButton("ساخت پشتیبان", callback_data="backup:create")]]
    buttons.append([InlineKeyboardButton("بازگشت به پنل", callback_data="admin:home")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")


async def _admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(context, query.from_user.id):
        await _deny(query)
        return
    await query.answer()
    try:
        parts = query.data.split(":")
        action, raw_id = (parts[0].removeprefix("report-"), parts[1]) if parts[0].startswith("report-") else (parts[1], parts[2])
        raw_id = int(raw_id)
        market: Marketplace = _market(context)
        if action in {"approve", "reject", "edits", "request", "archive", "restore"}:
            if action in {"archive", "restore"}:
                market.transition(raw_id, "archived" if action == "archive" else "draft", query.from_user.id)
                await query.edit_message_text(f"آگهی {raw_id} بایگانی شد." if action == "archive" else f"آگهی {raw_id} به پیش‌نویس برگشت.")
                return
            target = {"approve": "approved", "reject": "rejected", "edits": "rejected", "request": "rejected"}[action]
            note = "لطفاً معلومات آگهی را تکمیل یا اصلاح کرده و دوباره ثبت کنید." if action in {"edits", "request"} else ""
            market.transition(raw_id, target, query.from_user.id, note)
            ad = market.get_ad(raw_id)
            await query.edit_message_text(
                f"آگهی {raw_id}: {'تأیید شد' if action == 'approve' else 'برای تکمیل به فروشنده برگشت' if action in {'edits', 'request'} else 'رد شد'}."
            )
            if action == "approve":
                await _publish_ad(context, raw_id, query.from_user.id)
            elif ad:
                try:
                    await context.bot.send_message(ad["owner_id"], f"آگهی {raw_id} نیاز به اصلاح دارد. یادداشت مدیر: {note or 'با مدیران تماس بگیرید.'}")
                except TelegramError as exc:
                    logger.info("Unable to notify owner of ad review %s: %s", raw_id, type(exc).__name__)
        elif action in {"review", "dismiss"}:
            market.review_report(raw_id, query.from_user.id, action == "dismiss")
            await query.edit_message_text(f"گزارش {raw_id} بررسی شد.")
        else:
            await query.edit_message_text("گزینه معتبر نیست.")
    except (ValueError, IndexError, MarketplaceError) as exc:
        await query.edit_message_text(str(exc) if isinstance(exc, MarketplaceError) else "این درخواست معتبر نیست.")


async def _publish_ad(context: ContextTypes.DEFAULT_TYPE, ad_id: int, admin_id: int, manual_retry: bool = False) -> None:
    market: Marketplace = _market(context)
    config = context.application.bot_data["config"]
    if config.publication_chat_id is None:
        logger.warning("Publication blocked: no destination configured")
        return
    if not market.claim_publication(ad_id, admin_id, config.publication_chat_id, retry_failed=manual_retry):
        return
    ad = market.get_ad(ad_id)
    if not ad:
        market.fail_publication(ad_id, admin_id)
        return
    caption = f"{ad['title']} | آگهی #{ad_id}"
    details = format_public_ad(ad)
    try:
        photos = market.photos(ad_id)
        buttons = [[InlineKeyboardButton("⚠️ گزارش آگهی", callback_data=f"report:{ad_id}")]]
        username = ad["owner_username"]
        if username and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
            buttons.insert(0, [InlineKeyboardButton("💬 پیام به فروشنده", url=f"https://t.me/{username}")])
        publication_buttons = InlineKeyboardMarkup(buttons)
        if len(photos) > 1:
            media = [InputMediaPhoto(media=file_id, caption=caption if index == 0 else None) for index, file_id in enumerate(photos)]
            messages = await context.bot.send_media_group(config.publication_chat_id, media)
            message_ids = [message.message_id for message in messages]
            market.record_publication_messages(ad_id, config.publication_chat_id, message_ids)
            details_message = await context.bot.send_message(config.publication_chat_id, details, reply_markup=publication_buttons)
            market.add_publication_message(ad_id, config.publication_chat_id, details_message.message_id)
            market.finish_album_publication(ad_id, config.publication_chat_id, [*message_ids, details_message.message_id])
        elif photos:
            message = await context.bot.send_photo(config.publication_chat_id, photos[0], caption=caption)
            market.record_publication_message(ad_id, message.message_id, config.publication_chat_id)
            details_message = await context.bot.send_message(config.publication_chat_id, details, reply_markup=publication_buttons)
            market.add_publication_message(ad_id, config.publication_chat_id, details_message.message_id)
            market.finish_publication(ad_id, message.message_id, config.publication_chat_id)
        else:
            message = await context.bot.send_message(config.publication_chat_id, details, reply_markup=publication_buttons)
            market.finish_publication(ad_id, message.message_id, config.publication_chat_id)
        try:
            await context.bot.send_message(ad["owner_id"], f"آگهی {ad_id} در بازارچه نشر شد.")
        except TelegramError as exc:
            logger.info("Publication completed but owner notification failed for ad %s: %s", ad_id, type(exc).__name__)
    except TelegramError as exc:
        logger.warning("Publication attempt failed for ad %s: %s", ad_id, type(exc).__name__)
        market.fail_publication(ad_id, admin_id)


async def _settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(context, query.from_user.id):
        await _deny(query)
        return
    await query.answer()
    key = query.data.partition(":")[2]
    market = _market(context)
    try:
        if key == "links_allowed":
            value = "0" if market.get_setting(key, "1") == "1" else "1"
        elif key == "review_mode":
            value = "required"
        elif key == "welcome_enabled":
            value = "0" if market.get_setting(key, "1") == "1" else "1"
        elif key == "join_approval":
            value = "0" if market.get_setting(key, "0") == "1" else "1"
        elif key == "public_rankings":
            value = "0" if market.get_setting(key, "1") == "1" else "1"
        elif key == "blocked_phrases":
            value = market.get_setting(key)
            await query.edit_message_text(f"عبارت‌های بستهٔ کنونی: {html.escape(value) or 'ندارد'}\nبرای افزودن عبارت، /blockphrase متن را بزنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="admin:antispam")]]), parse_mode="HTML")
            return
        else:
            await query.edit_message_text("این تنظیم شناخته نشد.")
            return
        market.set_setting(key, value, query.from_user.id)
        destination = "publication" if key == "review_mode" else "reputation" if key == "public_rankings" else "group" if key in {"welcome_enabled", "join_approval"} else "antispam"
        labels = {
            "links_allowed": "اجازهٔ پیوند",
            "review_mode": "تأیید مدیر پیش از نشر",
            "welcome_enabled": "خوش‌آمدگویی",
            "join_approval": "تأیید مدیر برای درخواست‌های عضویت",
            "public_rankings": "رتبه‌بندی عمومی",
        }
        state = "روشن" if value == "1" else "خاموش"
        if key == "review_mode":
            state = "اجباری"
        await query.edit_message_text(
            f"{labels.get(key, key)}: {state}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data=f"admin:{destination}")]]),
        )
    except MarketplaceError as exc:
        await query.edit_message_text(str(exc))


async def _category_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(context, query.from_user.id):
        await _deny(query)
        return
    await query.answer()
    try:
        category_id = int(query.data.partition(":")[2])
        db = context.application.bot_data["database"]
        with db.transaction() as conn:
            cursor = conn.execute("UPDATE categories SET enabled=1-enabled WHERE category_id=?", (category_id,))
            if cursor.rowcount != 1: raise ValueError
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'category_toggle',?)", (query.from_user.id, str(category_id)))
        await query.edit_message_text("وضعیت دسته‌بندی تغییر کرد. برای دیدن فهرست دوباره /admin را بزنید.")
    except (ValueError, TypeError):
        await query.edit_message_text("دسته‌بندی پیدا نشد.")


async def _trust_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(context, query.from_user.id):
        await _deny(query)
        return
    await query.answer()
    try:
        user_id = int(query.data.partition(":")[2])
        trusted = _market(context).set_trusted(user_id, query.from_user.id)
        await query.edit_message_text(f"وضعیت اعتماد کاربر {user_id}: {'مورد اعتماد' if trusted else 'عادی'}.")
    except ValueError:
        await query.edit_message_text("شناسه معتبر نیست.")


async def _group_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(context, query.from_user.id):
        await _deny(query)
        return
    await query.answer()
    try:
        event_id = int(query.data.partition(":")[2])
        chat_id, message_id, username = _market(context).mark_group_message_reviewed(event_id, query.from_user.id)
        url = _group_message_url(chat_id, message_id, username)
        if url:
            await query.edit_message_text(
                f"پیام احتمالی آگهی #{event_id} بررسی‌شده ثبت شد؛ پیام اصلی حذف یا تغییر داده نشد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازکردن پیام", url=url)]]),
            )
        else:
            await query.edit_message_text(f"پیام احتمالی آگهی #{event_id} بررسی‌شده ثبت شد؛ پیام اصلی حذف یا تغییر داده نشد.")
    except (ValueError, MarketplaceError) as exc:
        await query.edit_message_text(str(exc) if isinstance(exc, MarketplaceError) else "شناسهٔ پیام معتبر نیست.")


async def join_request_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(context, query.from_user.id):
        await _deny(query)
        return
    await query.answer()
    try:
        _, decision, raw_chat_id, raw_user_id = query.data.split(":")
        chat_id, user_id = int(raw_chat_id), int(raw_user_id)
        if decision == "approve":
            await context.bot.approve_chat_join_request(chat_id, user_id)
        elif decision == "reject":
            await context.bot.decline_chat_join_request(chat_id, user_id)
        else:
            raise ValueError
        db = context.application.bot_data["database"]
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)",
                (query.from_user.id, f"join_request_{decision}", f"{chat_id}:{user_id}"),
            )
        await query.edit_message_text(f"درخواست عضویت کاربر {user_id}: {'پذیرفته شد' if decision == 'approve' else 'رد شد'}.")
    except (ValueError, TelegramError) as exc:
        logger.info("Join request decision failed: %s", type(exc).__name__)
        await query.edit_message_text("درخواست پیدا نشد، پایان یافته یا ربات اجازهٔ مدیریت آن را ندارد.")


async def _feature_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(context, query.from_user.id):
        await _deny(query)
        return
    await query.answer()
    try:
        ad_id = int(query.data.partition(":")[2])
        db = context.application.bot_data["database"]
        with db.transaction() as conn:
            cursor = conn.execute("UPDATE ads SET featured_until=CASE WHEN featured_until > strftime('%Y-%m-%dT%H:%M:%fZ','now') THEN NULL ELSE ? END WHERE ad_id=? AND status='published'", ((datetime.now(UTC) + timedelta(days=7)).isoformat(), ad_id))
            if cursor.rowcount != 1: raise ValueError
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'featured_toggle',?)", (query.from_user.id, str(ad_id)))
        await query.edit_message_text("وضعیت آگهی ویژه تغییر کرد.")
    except (ValueError, TypeError):
        await query.edit_message_text("آگهی نشرشده پیدا نشد.")


async def _backup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(context, query.from_user.id):
        await _deny(query)
        return
    await query.answer()
    config = context.application.bot_data["config"]
    try:
        path = await asyncio.to_thread(create_backup, context.application.bot_data["database"], config.backup_directory)
        with path.open("rb") as backup_file:
            await query.message.reply_document(backup_file, filename=path.name, caption="پشتیبان دیتابیس بازارچه؛ در محل امن نگهداری شود.")
    except (OSError, ValueError, sqlite3.Error) as exc:
        logger.warning("Backup failed: %s", type(exc).__name__)
        await query.message.reply_text("ساخت پشتیبان انجام نشد. مدیر سیستم باید دسترسی پوشه را بررسی کند.")


async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text("کاربرد: /warn شناسه_کاربر دلیل"); return
    count = context.application.bot_data["moderation"].warn(int(context.args[0]), update.effective_user.id, " ".join(context.args[1:]))
    await update.effective_message.reply_text(f"اخطار ثبت شد. شمار اخطارها: {count}.")


async def set_welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    value = " ".join(context.args).strip()
    if not value or len(value) > 3000:
        await update.effective_message.reply_text("متن خوش‌آمدگویی باید میان ۱ تا ۳۰۰۰ نویسه باشد. کاربرد: /welcome_set متن")
        return
    _market(context).set_setting("welcome_message", value, update.effective_user.id)
    await update.effective_message.reply_text("متن خوش‌آمدگویی ذخیره شد.")


def _target_args(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args or not context.args[0].isdigit():
        return None
    return int(context.args[0])


async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id): await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    target = _target_args(update, context)
    if not target or not context.args[1:]: await update.effective_message.reply_text("کاربرد: /mute شناسه_کاربر مدت_ثانیه دلیل"); return
    try: seconds = min(max(int(context.args[1]), 60), 2_592_000)
    except ValueError: await update.effective_message.reply_text("مدت باید به ثانیه باشد."); return
    group_id = context.application.bot_data["config"].group_id
    if not group_id: await update.effective_message.reply_text("GROUP_ID تنظیم نشده است."); return
    try:
        await context.bot.restrict_chat_member(group_id, target, ChatPermissions(can_send_messages=False), until_date=datetime.now(UTC) + timedelta(seconds=seconds))
        context.application.bot_data["moderation"].record_action(target, update.effective_user.id, "mute", " ".join(context.args[2:]))
        await update.effective_message.reply_text("کاربر برای مدت تعیین‌شده خاموش شد.")
    except TelegramError as exc:
        logger.warning("Mute failed: %s", type(exc).__name__); await update.effective_message.reply_text("خاموش‌کردن انجام نشد؛ دسترسی مدیر ربات در گروه را بررسی کنید.")


async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id): await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    target = _target_args(update, context); group_id = context.application.bot_data["config"].group_id
    if not target or not group_id: await update.effective_message.reply_text("کاربرد: /unmute شناسه_کاربر (و تنظیم GROUP_ID)"); return
    permissions = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True)
    try:
        await context.bot.restrict_chat_member(group_id, target, permissions)
        context.application.bot_data["moderation"].record_action(target, update.effective_user.id, "unmute")
        await update.effective_message.reply_text("محدودیت ارسال پیام برداشته شد.")
    except TelegramError as exc:
        logger.warning("Unmute failed: %s", type(exc).__name__); await update.effective_message.reply_text("رفع محدودیت انجام نشد؛ دسترسی مدیر ربات را بررسی کنید.")


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id): await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    target = _target_args(update, context); group_id = context.application.bot_data["config"].group_id
    if not target or not group_id: await update.effective_message.reply_text("کاربرد: /ban شناسه_کاربر (و تنظیم GROUP_ID)"); return
    try:
        await context.bot.ban_chat_member(group_id, target)
        context.application.bot_data["moderation"].record_action(target, update.effective_user.id, "ban", " ".join(context.args[1:]))
        await update.effective_message.reply_text("کاربر از گروه بیرون شد.")
    except TelegramError as exc:
        logger.warning("Ban failed: %s", type(exc).__name__); await update.effective_message.reply_text("بیرون‌کردن انجام نشد؛ دسترسی مدیر ربات را بررسی کنید.")


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id): await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    target = _target_args(update, context); group_id = context.application.bot_data["config"].group_id
    if not target or not group_id: await update.effective_message.reply_text("کاربرد: /unban شناسه_کاربر (و تنظیم GROUP_ID)"); return
    try:
        await context.bot.unban_chat_member(group_id, target, only_if_banned=True)
        context.application.bot_data["moderation"].record_action(target, update.effective_user.id, "unban")
        await update.effective_message.reply_text("محرومیت کاربر برداشته شد.")
    except TelegramError as exc:
        logger.warning("Unban failed: %s", type(exc).__name__); await update.effective_message.reply_text("رفع محرومیت انجام نشد؛ دسترسی مدیر ربات را بررسی کنید.")


async def category_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id): await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    name = " ".join(context.args).strip()
    if not name or len(name) > 80: await update.effective_message.reply_text("کاربرد: /category_add نام دسته (حداکثر ۸۰ نویسه)"); return
    try:
        db = context.application.bot_data["database"]
        with db.transaction() as conn:
            order = conn.execute("SELECT coalesce(max(sort_order),0)+1 FROM categories").fetchone()[0]
            conn.execute("INSERT INTO categories(name,sort_order) VALUES(?,?)", (name, order))
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'category_add',?)", (update.effective_user.id, name[:80]))
        await update.effective_message.reply_text("دسته‌بندی افزوده شد.")
    except sqlite3.IntegrityError:
        await update.effective_message.reply_text("این نام از پیش استفاده شده یا ذخیره‌سازی ناممکن است.")


async def category_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text("کاربرد: /category_rename شناسه نام تازه")
        return
    try:
        _market(context).rename_category(int(context.args[0]), " ".join(context.args[1:]), update.effective_user.id)
        await update.effective_message.reply_text("نام دسته‌بندی تغییر کرد.")
    except MarketplaceError as exc:
        await update.effective_message.reply_text(str(exc))


async def category_move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    if len(context.args) != 2 or not all(value.isdigit() for value in context.args):
        await update.effective_message.reply_text("کاربرد: /category_move شناسه جایگاه (جایگاه از ۱ آغاز می‌شود)")
        return
    try:
        _market(context).reorder_category(int(context.args[0]), int(context.args[1]), update.effective_user.id)
        await update.effective_message.reply_text("ترتیب دسته‌بندی‌ها ذخیره شد.")
    except MarketplaceError as exc:
        await update.effective_message.reply_text(str(exc))


async def set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id): await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    text = " ".join(context.args).strip()
    if not text or len(text) > 3000: await update.effective_message.reply_text("کاربرد: /rules_set متن مقررات"); return
    _market(context).set_setting("rules", text, update.effective_user.id)
    await update.effective_message.reply_text("مقررات ذخیره شد.")


async def block_phrase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id): await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    phrase = " ".join(context.args).strip()
    if not phrase or len(phrase) > 100: await update.effective_message.reply_text("کاربرد: /blockphrase عبارت (حداکثر ۱۰۰ نویسه)"); return
    phrases = [item for item in _market(context).get_setting("blocked_phrases").split("\n") if item]
    if phrase.casefold() not in {item.casefold() for item in phrases}: phrases.append(phrase)
    _market(context).set_setting("blocked_phrases", "\n".join(phrases), update.effective_user.id)
    await update.effective_message.reply_text("عبارت به فهرست بررسی افزوده شد؛ پیش از اقدام مدیر پیام را می‌بیند.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id): await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    with context.application.bot_data["database"].connect() as conn:
        users = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        ads = conn.execute("SELECT count(*) FROM ads").fetchone()[0]
        pending = conn.execute("SELECT count(*) FROM ads WHERE status='pending'").fetchone()[0]
        reports = conn.execute("SELECT count(*) FROM reports WHERE status='open'").fetchone()[0]
    await update.effective_message.reply_text(f"کاربران: {users}\nهمهٔ آگهی‌ها: {ads}\nدر انتظار بررسی: {pending}\nگزارش باز: {reports}")


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id): await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    db = context.application.bot_data["database"]
    with db.connect() as conn: result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    await update.effective_message.reply_text(f"ربات فعال است. سلامت دیتابیس: {result}.")


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id): await update.effective_message.reply_text("این فرمان برای مدیران است."); return
    config = context.application.bot_data["config"]
    try:
        path = await asyncio.to_thread(create_backup, context.application.bot_data["database"], config.backup_directory)
        with path.open("rb") as backup_file:
            await update.effective_message.reply_document(backup_file, filename=path.name, caption="پشتیبان امن دیتابیس بازارچه.")
    except (OSError, ValueError, sqlite3.Error) as exc:
        logger.warning("Backup failed: %s", type(exc).__name__); await update.effective_message.reply_text("ساخت پشتیبان انجام نشد.")


async def restore_database_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    if len(context.args) != 2 or context.args[1] != "تأیید" or not re.fullmatch(r"bazaarche-\d{8}T\d{12}Z\.sqlite3", context.args[0]):
        await update.effective_message.reply_text("کاربرد: /restore_db bazaarche-زمان.sqlite3 تأیید\nفایل باید در BACKUP_DIRECTORY باشد.")
        return
    config = context.application.bot_data["config"]
    source = config.backup_directory / context.args[0]
    try:
        before = await asyncio.to_thread(
            restore_backup, context.application.bot_data["database"], source,
            config.backup_directory, confirmed=True,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        logger.warning("Database restore rejected or failed: %s", type(exc).__name__)
        await update.effective_message.reply_text("بازیابی انجام نشد؛ دیتابیس فعلی دست‌نخورده نگه داشته شد. نام و سلامت فایل پشتیبان را بررسی کنید.")
        return
    await update.effective_message.reply_text(f"بازیابی انجام شد. نسخهٔ پیش از بازیابی با نام {before.name} در پوشهٔ پشتیبان نگهداری شد.")


async def reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    reports = _market(context).open_reports()
    if not reports:
        await update.effective_message.reply_text("گزارش بازی موجود نیست.")
        return
    for report in reports:
        buttons = [[InlineKeyboardButton("بررسی شد", callback_data=f"report-review:{report['report_id']}"), InlineKeyboardButton("رد گزارش", callback_data=f"report-dismiss:{report['report_id']}")]]
        await update.effective_message.reply_text(f"گزارش {report['report_id']} | آگهی {report['ad_id']}\n{html.escape(report['title'])}\nدسته: {html.escape(report['category'])}\nتوضیح: {html.escape(report['description'] or 'ندارد')}", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")


async def warnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("کاربرد: /warnings شناسه_کاربر")
        return
    user_id = int(context.args[0])
    with context.application.bot_data["database"].connect() as conn:
        rows = conn.execute("SELECT admin_id,reason,created_at FROM warnings WHERE user_id=? ORDER BY warning_id DESC LIMIT 20", (user_id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("برای این کاربر اخطاری ثبت نشده است.")
        return
    details = "\n".join(f"مدیر {row['admin_id']} | {row['created_at']}\n{row['reason']}" for row in rows)
    await update.effective_message.reply_text(f"اخطارهای کاربر {user_id}:\n{details}")


async def moderation_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("کاربرد: /modhistory شناسه_کاربر")
        return
    user_id = int(context.args[0])
    with context.application.bot_data["database"].connect() as conn:
        rows = conn.execute("SELECT admin_id,action,reason,created_at FROM moderation_events WHERE user_id=? ORDER BY event_id DESC LIMIT 20", (user_id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("سابقهٔ مدیریتی برای این کاربر ثبت نشده است.")
        return
    details = "\n".join(f"{row['created_at']} | {row['action']} | مدیر {row['admin_id'] or 'ربات'}\n{row['reason']}" for row in rows)
    await update.effective_message.reply_text(f"سابقهٔ کاربر {user_id}:\n{details}")


async def restore_ad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("کاربرد: /restore شناسه_آگهی")
        return
    try:
        _market(context).transition(int(context.args[0]), "draft", update.effective_user.id, "بازگردانی مدیر")
        await update.effective_message.reply_text("آگهی به پیش‌نویس بازگشت.")
    except MarketplaceError as exc:
        await update.effective_message.reply_text(str(exc))


async def retry_publication(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    if len(context.args) != 2 or not context.args[0].isdigit() or context.args[1] != "بررسی_شد":
        await update.effective_message.reply_text("کاربرد: /retry_publish شناسه_آگهی بررسی_شد\nاین تأیید را فقط پس از بررسی مقصد نشر بنویسید.")
        return
    ad_id = int(context.args[0])
    ad = _market(context).get_ad(ad_id)
    if not ad or ad["status"] != "publication_failed":
        await update.effective_message.reply_text("این آگهی در وضعیت خطای نشر نیست.")
        return
    await update.effective_message.reply_text("پیش از تلاش دوباره، مقصد نشر را بررسی کنید تا آگهی قبلاً نشر نشده باشد.")
    await _publish_ad(context, ad_id, update.effective_user.id, manual_retry=True)


async def confirm_partial_publication(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    if not context.args or len(context.args) > 12 or not context.args[0].isdigit() or not all(value.isdigit() for value in context.args[1:]):
        await update.effective_message.reply_text("کاربرد: /confirm_published شناسه_آگهی [شناسهٔ_پیام ...]\nشناسه‌های پیام را پس از بررسی مقصد بنویسید.")
        return
    try:
        message_ids = [int(value) for value in context.args[1:]] or None
        _market(context).confirm_partial_publication(int(context.args[0]), update.effective_user.id, message_ids)
        await update.effective_message.reply_text("نشر قبلی ثبت شد؛ پیام تازه فرستاده نشد.")
    except MarketplaceError as exc:
        await update.effective_message.reply_text(str(exc))


async def publish_approved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("این فرمان برای مدیران است.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("کاربرد: /publish شناسه_آگهی")
        return
    ad_id = int(context.args[0])
    ad = _market(context).get_ad(ad_id)
    if not ad or ad["status"] != "approved":
        await update.effective_message.reply_text("این آگهی تأییدشده و آمادهٔ نشر نیست.")
        return
    await _publish_ad(context, ad_id, update.effective_user.id)
    await update.effective_message.reply_text(f"وضعیت نشر آگهی {ad_id}: {_market(context).get_ad(ad_id)['status']}.")


def admin_handlers() -> list:
    return [
        CommandHandler("admin", admin_panel), CommandHandler("stats", stats), CommandHandler("health", health),
        CommandHandler("backup", backup_command), CommandHandler("warn", warn_user),
        CommandHandler("restore_db", restore_database_command),
        CommandHandler("reports", reports_command), CommandHandler("warnings", warnings_command),
        CommandHandler("modhistory", moderation_history), CommandHandler("restore", restore_ad),
        CommandHandler("retry_publish", retry_publication),
        CommandHandler("confirm_published", confirm_partial_publication),
        CommandHandler("publish", publish_approved),
        CommandHandler("mute", mute_user), CommandHandler("unmute", unmute_user),
        CommandHandler("ban", ban_user), CommandHandler("unban", unban_user),
        CommandHandler("category_add", category_add), CommandHandler("rules_set", set_rules),
        CommandHandler("category_rename", category_rename), CommandHandler("category_move", category_move),
        CommandHandler("blockphrase", block_phrase),
        CommandHandler("welcome_set", set_welcome_message),
        CallbackQueryHandler(_dashboard_callback, pattern=r"^admin:[a-z_]+$"),
        CallbackQueryHandler(_admin_action, pattern=r"^(ad:(approve|reject|edits|request|archive|restore):\d+|report-(review|dismiss):\d+)$"),
        CallbackQueryHandler(_group_review_action, pattern=r"^group-review:\d+$"),
        CallbackQueryHandler(join_request_decision, pattern=r"^join:(approve|reject):-?\d+:\d+$"),
        CallbackQueryHandler(_settings, pattern=r"^setting:(links_allowed|review_mode|blocked_phrases|public_rankings|welcome_enabled|join_approval)$"),
        CallbackQueryHandler(_category_toggle, pattern=r"^category:\d+$"),
        CallbackQueryHandler(_trust_toggle, pattern=r"^trust:\d+$"),
        CallbackQueryHandler(_feature_toggle, pattern=r"^featured:\d+$"),
        CallbackQueryHandler(_backup_callback, pattern=r"^backup:create$"),
    ]