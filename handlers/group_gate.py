"""Group-only enforcement, private referral links and owner-controlled activation."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import OrderedDict
from types import SimpleNamespace
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from content import SUPPORT_URL
from handlers.panel import render
from services.group_policy import GroupPolicy, classify_group_ad

logger = logging.getLogger(__name__)


def policy(context) -> GroupPolicy:
    return context.application.bot_data["group_policy"]


def member_present(member) -> bool:
    return (
        member.status
        in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
        or member.status == ChatMemberStatus.RESTRICTED
        and bool(getattr(member, "is_member", False))
    )


async def is_exempt(context, chat_id: int, user_id: int) -> bool | None:
    config = context.application.bot_data["config"]
    if user_id in config.admin_ids or user_id == context.bot.id:
        return True
    cache = context.application.bot_data.setdefault("group_roles", OrderedDict())
    key = (chat_id, user_id)
    now = time.monotonic()
    cached = cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        result = member.status in {
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
    except TelegramError as exc:
        logger.info("Group role lookup failed: %s", type(exc).__name__)
        return None  # Never delete when administrator exemption cannot be checked.
    cache[key] = (result, now + 30)
    cache.move_to_end(key)
    if len(cache) > 4096:
        cache.popitem(last=False)
    return result


async def required_permissions(context, include_invites: bool) -> bool:
    config = context.application.bot_data["config"]
    if config.group_id is None:
        return False
    try:
        member = await context.bot.get_chat_member(config.group_id, context.bot.id)
    except TelegramError:
        return False
    return bool(
        member.status == ChatMemberStatus.OWNER
        or member.status == ChatMemberStatus.ADMINISTRATOR
        and member.can_delete_messages
        and (not include_invites or member.can_invite_users)
    )


async def destinations(context):
    bot = await context.bot.get_me()
    base = f"https://t.me/{bot.username}"
    app = (
        base + "?startapp"
        if getattr(bot, "has_main_web_app", False)
        else base + "?start=menu"
    )
    return base + "?start=invite", app


async def delete_notice(context):
    data = context.job.data
    try:
        await context.bot.delete_message(data["chat_id"], data["message_id"])
    except TelegramError:
        pass


async def warn_member(context, chat_id: int, user, reason: str):
    p = policy(context)
    if not p.claim_notice(chat_id, user.id):
        return
    invite_url, app_url = await destinations(context)
    name = html.escape(user.first_name or "دوست عزیز")
    mention = f'<a href="tg://user?id={user.id}">{name}</a>' if user.id > 0 else name
    rows = []
    if reason == "invite_required":
        needed = p.settings(chat_id)["required_invites"]
        count = p.count(chat_id, user.id)
        text = f"{mention}، برای فعالیت در گروه {needed} دوست را با لینک اختصاصی خود دعوت کن.\nدعوت ثبت‌شده: {count}؛ باقی‌مانده: {max(0,needed-count)}.\nآگهی‌ها فقط از مینی‌اپ ثبت می‌شوند."
        rows.append([InlineKeyboardButton("👥 لینک دعوت و وضعیت من", url=invite_url)])
    elif reason == "identity_required":
        text = f"{mention}، برای بررسی شرط دعوت، با حساب شخصی خود پیام بفرست؛ هویت فرستندهٔ کانالی به عضو گروه قابل نسبت‌دادن نیست."
        rows.append([InlineKeyboardButton("👥 لینک دعوت و وضعیت من", url=invite_url)])
    else:
        text = f"{mention}، ثبت آگهی فروش، درخواست خرید و معرفی خدمات از مینی‌اپ انجام می‌شود؛ پس از تأیید مدیر در گروه نشر می‌شود."
    rows.append([InlineKeyboardButton("🛍 ثبت آگهی در بازارچه", url=app_url)])
    try:
        sent = await context.bot.send_message(
            chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
        )
        if context.job_queue:
            context.job_queue.run_once(
                delete_notice,
                45,
                data={"chat_id": chat_id, "message_id": sent.message_id},
            )
    except TelegramError as exc:
        logger.info("Group guidance unavailable: %s", type(exc).__name__)


def re_newad(text):
    return (
        text.split(maxsplit=1)[0].split("@")[0].lower() == "/newad"
        if text.strip()
        else False
    )


async def enforce_group_message(update: Update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    config = context.application.bot_data["config"]
    # Destructive actions are confined to the one configured group, never private chats or other groups.
    if (
        not message
        or not chat
        or chat.type not in {"group", "supergroup"}
        or config.group_id is None
        or chat.id != config.group_id
    ):
        return
    p = policy(context)
    settings = p.settings(chat.id)
    sender = getattr(message, "sender_chat", None)
    if sender and (
        sender.id == chat.id or getattr(message, "is_automatic_forward", False)
    ):
        return  # Anonymous administrators and linked-channel automatic forwards.
    if sender:
        user = SimpleNamespace(id=sender.id, first_name=sender.title)
        reason = (
            "identity_required"
            if settings["invite_required"]
            else (
                "direct_ad"
                if settings["ad_delete"]
                and classify_group_ad(
                    message.text or message.caption or "", bool(message.photo)
                )
                == "ad"
                else None
            )
        )
    else:
        if not user or getattr(user, "is_bot", False):
            return
        p.seen(chat.id, user.id)
        if not settings["invite_required"] and not settings["ad_delete"]:
            return
        exempt = await is_exempt(context, chat.id, user.id)
        if exempt is not False:
            return
        reason = None
        if (
            settings["invite_required"]
            and p.count(chat.id, user.id) < settings["required_invites"]
        ):
            reason = "invite_required"
        elif settings["ad_delete"] and (
            classify_group_ad(
                message.text or message.caption or "", bool(message.photo)
            )
            == "ad"
            or re_newad(message.text or "")
        ):
            reason = "direct_ad"
    if p.deleted(chat.id, message.message_id):
        raise ApplicationHandlerStop
    if not reason:
        if (
            not sender
            and settings["ad_delete"]
            and classify_group_ad(
                message.text or message.caption or "", bool(message.photo)
            )
            == "review"
        ):
            market = context.application.bot_data["marketplace"]
            market.ensure_user(
                user.id, getattr(user, "username", None), getattr(user, "full_name", "")
            )
            event_id, created = market.flag_group_message(
                user.id, chat.id, message.message_id, getattr(chat, "username", None)
            )
            notify = context.application.bot_data.get("notify_group_review")
            if created and notify:
                await notify(
                    context,
                    event_id,
                    chat.id,
                    message.message_id,
                    getattr(chat, "title", "گروه"),
                    "review_group_ad",
                )
        return
    try:
        await context.bot.delete_message(chat.id, message.message_id)
    except TelegramError as exc:
        logger.warning("Group message deletion unavailable: %s", type(exc).__name__)
        return  # Leave the message and existing review path intact; never pretend deletion succeeded.
    p.record_delete(chat.id, message.message_id, user.id, reason)
    try:
        await warn_member(context, chat.id, user, reason)
    except TelegramError as exc:
        logger.info("Group guidance failed: %s", type(exc).__name__)
    raise ApplicationHandlerStop


async def membership_update(update: Update, context):
    event = update.chat_member
    config = context.application.bot_data["config"]
    if not event or config.group_id is None or event.chat.id != config.group_id:
        return
    person = event.new_chat_member.user
    if person.is_bot:
        return
    p = policy(context)
    context.application.bot_data.setdefault("group_roles", OrderedDict()).pop(
        (event.chat.id, person.id), None
    )
    if member_present(event.old_chat_member):
        p.seen(event.chat.id, person.id)
        return
    if not member_present(event.new_chat_member):
        return
    link = getattr(event, "invite_link", None)
    inviter = p.register_join(
        event.chat.id,
        person.id,
        link.invite_link if link else None,
        bool(getattr(event, "via_join_request", False)),
        int(event.date.timestamp()),
    )
    if inviter and p.settings(event.chat.id)["invite_required"]:
        count = p.count(event.chat.id, inviter)
        required = p.settings(event.chat.id)["required_invites"]
        if count == required:
            try:
                await context.bot.send_message(
                    inviter,
                    f"✅ {required} دعوت معتبر ثبت شد؛ اکنون می‌توانی در گروه گفت‌وگو کنی.\nبرای ثبت آگهی همچنان از مینی‌اپ استفاده کن.",
                )
            except TelegramError:
                pass


async def invite_status(update: Update, context):
    if update.callback_query:
        await update.callback_query.answer()
    if not update.effective_chat or update.effective_chat.type != "private":
        if update.effective_message:
            await render(update, context,
                "برای دریافت لینک اختصاصی، /invites را در چت خصوصی ربات بفرست."
            )
        return
    config = context.application.bot_data["config"]
    user = update.effective_user
    if config.group_id is None:
        await render(update, context,
            "گروه بازارچه هنوز توسط مدیر تنظیم نشده است."
        )
        return
    p = policy(context)
    settings = p.settings(config.group_id)
    try:
        member = await context.bot.get_chat_member(config.group_id, user.id)
        if member_present(member):
            async with context.application.bot_data.setdefault(
                "invite_link_lock", asyncio.Lock()
            ):
                link = p.link(config.group_id, user.id)
                if not link:
                    created = await context.bot.create_chat_invite_link(
                        config.group_id,
                        name=f"bazarcha-{user.id}",
                        creates_join_request=context.application.bot_data[
                            "marketplace"
                        ].get_setting("join_approval", "0")
                        == "1",
                    )
                    link = created.invite_link
                    p.save_link(config.group_id, user.id, link)
    except TelegramError as exc:
        logger.info("Invitation link unavailable: %s", type(exc).__name__)
        await render(update, context,
            "لینک دعوت فعلاً در دسترس نیست؛ مدیر باید دسترسی دعوت اعضا را بررسی کند.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💬 پشتیبانی", url=SUPPORT_URL)]]
            ),
        )
        return
    if not member_present(member):
        await render(update, context,
            "ابتدا عضو گروه بازارچه شو؛ سپس لینک دعوت اختصاصی دریافت کن."
        )
        return
    count = p.count(config.group_id, user.id)
    required = settings["required_invites"]
    text = f"👥 لینک اختصاصی دعوت شما:\n{link}\n\nدعوت معتبر: {count}\nباقی‌مانده: {max(0,required-count)}\n"
    text += (
        "✅ شرط دعوت تکمیل شده است."
        if count >= required
        else f"سه دوست متفاوت و غیرربات را از همین لینک دعوت کن."
    )
    if not settings["invite_required"]:
        text += "\nℹ️ شرط دعوت برای گفت‌وگو فعلاً خاموش است."
    text += "\nورود تکراری امتیاز تازه ندارد. دعوت‌های پیش از فعال‌شدن این سیستم قابل بازیابی نیستند."
    await render(update, context,
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 بررسی وضعیت دعوت‌ها", callback_data="group:invites"
                    )
                ],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")],
            ]
        ),
    )


def panel_text(context):
    config = context.application.bot_data["config"]
    settings = policy(context).settings(config.group_id or 0)
    return (
        "مدیریت فعالیت گروه\n"
        f'شرط {settings["required_invites"]} دعوت: '
        + ("روشن" if settings["invite_required"] else "خاموش")
        + "\n"
        "حذف آگهی مستقیم: " + ("روشن" if settings["ad_delete"] else "خاموش") + "\n\n"
        "با روشن‌شدن شرط دعوت، پیام اعضای بدون دعوت کافی حذف می‌شود؛ شامل اعضای قبلی هم می‌شود.\n"
        "حذف آگهی فقط برای متن روشنِ فروش، خرید یا خدمات است؛ پیام مبهم برای بررسی مدیر می‌ماند.\n"
        "مدیران واقعی گروه و صاحب ربات مستثنا هستند. سه دعوت متفاوت یک‌بار شمرده می‌شوند و با خروج بعدی مهمان کم نمی‌شوند."
    )


def panel_keyboard(context):
    config = context.application.bot_data["config"]
    settings = policy(context).settings(config.group_id or 0)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ("خاموش‌کردن" if settings["invite_required"] else "روشن‌کردن")
                    + " شرط سه دعوت",
                    callback_data="gate:invite_required:"
                    + ("off" if settings["invite_required"] else "on"),
                )
            ],
            [
                InlineKeyboardButton(
                    ("خاموش‌کردن" if settings["ad_delete"] else "روشن‌کردن")
                    + " حذف آگهی مستقیم",
                    callback_data="gate:ad_delete:"
                    + ("off" if settings["ad_delete"] else "on"),
                )
            ],
            [InlineKeyboardButton("🔄 بررسی تنظیمات", callback_data="gate:panel")],
            [InlineKeyboardButton("بازگشت به مدیریت", callback_data="admin:home")],
        ]
    )


async def render_panel(update, context):
    try:
        await render(update, context,
            panel_text(context), reply_markup=panel_keyboard(context)
        )
    except BadRequest as exc:
        if not str(exc).casefold().startswith("message is not modified"):
            raise


async def admin_gate_callback(update: Update, context):
    query = update.callback_query
    config = context.application.bot_data["config"]
    if query.from_user.id not in config.admin_ids:
        await query.answer("فقط مدیر ربات اجازهٔ تغییر دارد.", show_alert=True)
        return
    if not config.group_id:
        await query.answer()
        await render(update, context, "ابتدا GROUP_ID را برای گروه بازارچه تنظیم کنید.")
        return
    if query.data != "gate:panel":
        _, field, mode = query.data.split(":")
        if field not in {"invite_required", "ad_delete"} or mode not in {"on", "off"}:
            await query.answer()
            return
        if mode == "on" and not await required_permissions(
            context,
            include_invites=field == "invite_required"
            or bool(policy(context).settings(config.group_id)["invite_required"]),
        ):
            await query.answer(
                "فعال نشد. ربات باید در گروه مدیر باشد و اجازهٔ حذف پیام داشته باشد؛ برای شرط دعوت اجازهٔ دعوت اعضا هم لازم است.",
                show_alert=True,
            )
            return
        policy(context).set_enabled(
            config.group_id, field, mode == "on", query.from_user.id
        )
    await query.answer()
    await render_panel(update, context)


def handlers():
    return [
        CommandHandler("invites", invite_status),
        CallbackQueryHandler(invite_status, pattern=r"^group:invites$"),
        CallbackQueryHandler(
            admin_gate_callback,
            pattern=r"^gate:(panel|(?:invite_required|ad_delete):(?:on|off))$",
        ),
    ]


def install_prehandlers(application):
    application.add_handler(
        ChatMemberHandler(membership_update, ChatMemberHandler.CHAT_MEMBER), group=-2
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, enforce_group_message
        ),
        group=-1,
    )
