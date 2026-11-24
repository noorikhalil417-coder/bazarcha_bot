"""Dari user commands and persistent advertisement submission flow."""

from __future__ import annotations

import html
import re

from content import SUPPORT_URL, CHARKHAK_URL
from handlers.panel import render

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from services.action_locks import serialized
from services.marketplace import Marketplace, MarketplaceError, category_emoji, format_price, parse_price_input

CATEGORY, AD_TYPE, TITLE, DESCRIPTION, PRICE, CITY, CONTACT, PHOTOS, CONFIRM = range(9)
EDIT_FIELD, EDIT_VALUE = range(9, 11)
TYPE_LABELS = {"sale": "فروش", "wanted": "خرید", "rental": "کرایه", "service": "خدمات"}
STATUS_LABELS = {"draft": "پیش‌نویس", "pending": "در انتظار بررسی", "approved": "تأییدشده", "published": "نشرشده", "rejected": "ردشده", "expired": "پایان‌یافته", "archived": "بایگانی‌شده", "sold": "فروخته‌شده", "publication_failed": "مشکل در نشر"}
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _market(context: ContextTypes.DEFAULT_TYPE) -> Marketplace:
    return context.application.bot_data["marketplace"]


def _touch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        _market(context).ensure_user(user.id, user.username, user.full_name)


def _current_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return _market(context).draft_for_user(update.effective_user.id)


def _ad_status_label(ad) -> str:
    if ad["status"] == "rejected" and ad["review_note"]:
        return "نیازمند اصلاح"
    return STATUS_LABELS.get(ad["status"], ad["status"])


def _main_menu(context=None, update=None) -> tuple[str, InlineKeyboardMarkup]:
    text = "به دوکانچه خوش آمدید! از گزینه‌های زیر انتخاب کنید:"
    buttons = [
        [InlineKeyboardButton("🔎 جست‌وجوی آگهی‌ها", callback_data="menu:search")],
        [InlineKeyboardButton("📂 دسته‌بندی آگهی‌ها", callback_data="menu:categories"),
         InlineKeyboardButton("📋 قوانین دوکانچه", callback_data="menu:rules")],
        [InlineKeyboardButton("📋 آگهی‌های من", callback_data="own:list:0")],
        [InlineKeyboardButton("💬 کمک و همکاری", callback_data="menu:help")],
        [InlineKeyboardButton("🎡 بازی چرخک", url=CHARKHAK_URL)],
    ]
    if context and update and getattr(getattr(update, "effective_chat", None), "type", None) == "private":
        url = context.application.bot_data["config"].mini_app_url
        if url:
            buttons.insert(0, [InlineKeyboardButton("✨ دوکانچه | ثبت آسان آگهی", web_app=WebAppInfo(url))])
    return text, InlineKeyboardMarkup(buttons)


def _wizard_buttons(back_to: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ بازگشت", callback_data=f"ad:back:{back_to}"),
        InlineKeyboardButton("لغو", callback_data="ad:cancel"),
    ]])


async def _prompt(update: Update, context, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    await render(update, context, text, reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _touch(update, context)
    if getattr(context, "args", None) == ["invite"]:
        from handlers.group_gate import invite_status
        await invite_status(update, context)
        return
    text, keyboard = _main_menu(context, update)
    await render(update, context, text, reply_markup=keyboard)


def help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 ارتباط با خلیل نوری", url=SUPPORT_URL)],
        [InlineKeyboardButton("👥 دعوت دوستان و وضعیت من", callback_data="group:invites")],
        [InlineKeyboardButton("ثبت آگهی بدون مینی‌اپ", callback_data="newad")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")],
    ])


def effective_rules(context):
    config = context.application.bot_data["config"]
    text = _market(context).get_setting("rules", config.group_rules)
    group_policy = context.application.bot_data.get("group_policy")
    if group_policy and config.group_id:
        settings = group_policy.settings(config.group_id)
        if settings["invite_required"]:
            text += f"\n\n👥 برای گفت‌وگو در گروه، {settings['required_invites']} دعوت متفاوت از لینک اختصاصی لازم است. در بخش کمک، «دعوت دوستان» را بزنید."
    return text


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await render(update, context,
        "💬 کمک و همکاری\nبرای پشتیبانی یا همکاری، با خلیل نوری تماس بگیرید.\nثبت آگهی از مینی‌اپ انجام می‌شود؛ اگر باز نشد، روش جایگزین را انتخاب کنید.",
        reply_markup=help_keyboard(),
    )


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = effective_rules(context)
    await render(update, context,
        "📋 مقررات دوکانچه:\n" + text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")]]),
    )


async def rankings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    market = _market(context)
    if market.get_setting("public_rankings", "1") != "1":
        await render(update, context, "نمایش همگانی رتبه‌بندی از سوی مدیران خاموش شده است.")
        return
    rows = market.seller_rankings()
    if not rows:
        await render(update, context, "در هفت روز گذشته امتیاز تأییدشده‌ای برای رتبه‌بندی ثبت نشده است.")
        return
    lines = ["فعالیت فروشندگان در هفت روز گذشته (امتیاز، گواه اعتماد نیست):"]
    lines.extend(f"{index}. {html.escape(row['display_name'] or row['username'] or 'فروشنده')} | {row['score']} امتیاز | {row['approved_ads']} آگهی تأییدشده" for index, row in enumerate(rows, 1))
    await render(update, context, "\n".join(lines), parse_mode="HTML")


async def _begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    chat = getattr(update, "effective_chat", None)
    if chat and chat.type != "private":
        bot = await context.bot.get_me()
        await render(update, context,
            "برای ثبت آگهی، مینی‌اپ را در چت خصوصی باز کن.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍 ورود به دوکانچه", url=f"https://t.me/{bot.username}?start=menu")]]),
        )
        return ConversationHandler.END
    _touch(update, context)
    draft = _current_draft(update, context)
    if draft:
        context.user_data["draft_id"] = draft["ad_id"]
        if not draft["title"]:
            await _prompt(update, context, "پیش‌نویس قبلی ادامه می‌یابد. عنوان آگهی را بنویسید؛ تا ۱۲۰ نویسه.", _wizard_buttons("type"))
            return TITLE
        if not draft["description"]:
            await _prompt(update, context, "پیش‌نویس قبلی ادامه می‌یابد. تشریح آگهی را بنویسید؛ تا ۳۰۰۰ نویسه.", _wizard_buttons("title"))
            return DESCRIPTION
        if draft["price_minor"] is None and not draft["price_negotiable"]:
            await _prompt(update, context, "پیش‌نویس قبلی ادامه می‌یابد. قیمت را به افغانی بنویسید؛ نمونه: «۳۰۰۰ افغانی»، «۳ هزار»، «با جورآمد» یا «توافقی».", _wizard_buttons("description"))
            return PRICE
        if not draft["city"]:
            await _prompt(update, context, "پیش‌نویس قبلی ادامه می‌یابد. شهر یا محل را بنویسید.", _wizard_buttons("price"))
            return CITY
        if not draft["contact"]:
            await _prompt(update, context, "پیش‌نویس قبلی ادامه می‌یابد. راه تماس را بنویسید.", _wizard_buttons("city"))
            return CONTACT
        return await _show_confirmation(update, context)
    categories = _market(context).categories()
    buttons = [[InlineKeyboardButton(f"{category_emoji(row['name'])} {row['name']}", callback_data=f"adcat:{row['category_id']}")] for row in categories]
    buttons.append([InlineKeyboardButton("لغو و بازگشت به منو", callback_data="ad:back:home")])
    await _prompt(update, context, "دسته‌بندی آگهی را انتخاب کنید:", InlineKeyboardMarkup(buttons))
    return CATEGORY


async def _select_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        context.user_data["ad_category"] = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await render(update, context, "دسته‌بندی معتبر نیست. دوباره از منوی اصلی آغاز کنید.", reply_markup=_wizard_buttons("home"))
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(label, callback_data=f"adtype:{key}")] for key, label in TYPE_LABELS.items()]
    buttons.extend(_wizard_buttons("category").inline_keyboard)
    await render(update, context, "نوع آگهی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))
    return AD_TYPE


@serialized("market")
async def _select_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    kind = query.data.partition(":")[2]
    if kind not in TYPE_LABELS:
        await render(update, context, "نوع آگهی معتبر نیست. دوباره آغاز کنید.", reply_markup=_wizard_buttons("category"))
        return ConversationHandler.END
    try:
        ad_id = _market(context).create_draft(update.effective_user.id, context.user_data.pop("ad_category"), kind)
    except (MarketplaceError, KeyError) as exc:
        await render(update, context, str(exc) if isinstance(exc, MarketplaceError) else "روند ثبت آگهی را دوباره آغاز کنید.")
        return ConversationHandler.END
    context.user_data["draft_id"] = ad_id
    await render(update, context, "عنوان آگهی را بنویسید؛ تا ۱۲۰ نویسه.", reply_markup=_wizard_buttons("type"))
    return TITLE


async def _save_text(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, next_state: int, prompt: str, back_to: str) -> int:
    _touch(update, context)
    draft = _current_draft(update, context)
    if not draft:
        await render(update, context, "پیش‌نویس پیدا نشد. لطفاً از منوی اصلی دوباره آغاز کنید.")
        return ConversationHandler.END
    try:
        _market(context).update_draft_field(draft["ad_id"], update.effective_user.id, field, update.effective_message.text)
    except MarketplaceError as exc:
        await render(update, context, str(exc), reply_markup=_wizard_buttons(back_to))
        return {"title": TITLE, "description": DESCRIPTION, "city": CITY, "contact": CONTACT}[field]
    await render(update, context, prompt, reply_markup=_wizard_buttons(back_to))
    return next_state


@serialized("market")
async def _title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if len(update.effective_message.text.strip()) > 120:
        await render(update, context, "عنوان باید حداکثر ۱۲۰ نویسه باشد. کوتاه‌تر بنویسید.", reply_markup=_wizard_buttons("type"))
        return TITLE
    return await _save_text(update, context, "title", DESCRIPTION, "تشریح آگهی را بنویسید؛ تا ۳۰۰۰ نویسه.", "title")


@serialized("market")
async def _description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if len(update.effective_message.text.strip()) > 3000:
        await render(update, context, "تشریح باید حداکثر ۳۰۰۰ نویسه باشد.", reply_markup=_wizard_buttons("title"))
        return DESCRIPTION
    return await _save_text(update, context, "description", PRICE, "قیمت را به افغانی بنویسید؛ نمونه: «۳۰۰۰ افغانی» یا «۳ هزار». برای جورآمد/توافقی همان عبارت را بنویسید.", "description")


@serialized("market")
async def _price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = _current_draft(update, context)
    if not draft:
        await render(update, context, "پیش‌نویس پیدا نشد. از منوی اصلی دوباره آغاز کنید.")
        return ConversationHandler.END
    value = update.effective_message.text.strip()
    try:
        amount, negotiable = parse_price_input(value)
    except MarketplaceError as exc:
        await render(update, context, str(exc), reply_markup=_wizard_buttons("description"))
        return PRICE
    _market(context).update_draft_field(draft["ad_id"], update.effective_user.id, "price_minor", amount)
    _market(context).update_draft_field(draft["ad_id"], update.effective_user.id, "price_negotiable", int(negotiable))
    await render(update, context, "شهر یا محل آگهی را بنویسید.", reply_markup=_wizard_buttons("price"))
    return CITY


@serialized("market")
async def _city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _save_text(update, context, "city", CONTACT, "راه تماس مورد تأیید برای نشر عمومی را بنویسید.", "city")


@serialized("market")
async def _contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    result = await _save_text(update, context, "contact", PHOTOS, "عکس‌های آگهی را بفرستید (حداکثر ۱۰ عکس). اگر عکس ندارید، دکمهٔ «پایان عکس‌ها» را بزنید.", "contact")
    if result == PHOTOS:
        buttons = [[InlineKeyboardButton("پایان عکس‌ها", callback_data="photos-done")], *_wizard_buttons("contact").inline_keyboard]
        await render(update, context, "عکس‌های آگهی اختیاری است؛ حداکثر ۱۰ عکس بفرستید یا «پایان عکس‌ها» را بزنید.", reply_markup=InlineKeyboardMarkup(buttons))
    return result


@serialized("market")
async def _photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = _current_draft(update, context)
    if not draft:
        await render(update, context, "پیش‌نویس پیدا نشد؛ از منوی اصلی دوباره آغاز کنید.")
        return ConversationHandler.END
    try:
        _market(context).add_photo(draft["ad_id"], update.effective_user.id, update.effective_message.photo[-1].file_id)
    except MarketplaceError as exc:
        buttons = [[InlineKeyboardButton("پایان عکس‌ها", callback_data="photos-done")], *_wizard_buttons("contact").inline_keyboard]
        await render(update, context, str(exc), reply_markup=InlineKeyboardMarkup(buttons))
    else:
        buttons = [[InlineKeyboardButton("پایان عکس‌ها", callback_data="photos-done")], *_wizard_buttons("contact").inline_keyboard]
        await render(update, context, "عکس افزوده شد. عکس دیگر بفرستید یا «پایان عکس‌ها» را بزنید.", reply_markup=InlineKeyboardMarkup(buttons))
    return PHOTOS


async def _photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return await _show_confirmation(update, context, query=True)


async def _show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, query: bool = False) -> int:
    draft = _current_draft(update, context)
    if not draft:
        text, markup = "پیش‌نویس پیدا نشد. دوباره /newad را بزنید.", None
    else:
        category = next((item["name"] for item in _market(context).categories(False) if item["category_id"] == draft["category_id"]), "نامعلوم")
        price = format_price(draft)
        text = (
            f"پیش‌نمایش آگهی:\n{category_emoji(category)} {html.escape(draft['title'])}\n"
            f"دسته: {html.escape(category)} | نوع: {TYPE_LABELS[draft['ad_type']]}\n"
            f"قیمت: {html.escape(price)}\nموقعیت: {html.escape(draft['city'])}\n"
            f"توضیحات: {html.escape(draft['description'])}\n"
            f"راه تماس (برای عموم نشر می‌شود): {html.escape(draft['contact'])}\n"
            f"عکس‌ها: {len(_market(context).photos(draft['ad_id']))}\n\n"
            "اطلاعات را تأیید می‌کنید؟"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأیید اطلاعات", callback_data="ad-submit"),
             InlineKeyboardButton("✏️ اصلاح", callback_data=f"owner:edit:{draft['ad_id']}")],
            [InlineKeyboardButton("لغو", callback_data="ad:cancel"),
             InlineKeyboardButton("⬅️ بازگشت", callback_data="ad:back:photos")],
        ])
    await render(update, context, text, reply_markup=markup, parse_mode="HTML")
    return CONFIRM if draft else ConversationHandler.END


@serialized("market")
async def _edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        ad_id = int(query.data.split(":", 2)[2])
        ad = _market(context).owned_ad(ad_id, query.from_user.id)
        if not ad or ad["status"] not in {"draft", "rejected", "expired", "archived"}:
            raise MarketplaceError("این آگهی قابل ویرایش نیست.")
        if ad["status"] != "draft":
            _market(context).reopen(ad_id, query.from_user.id)
    except (ValueError, MarketplaceError, IndexError) as exc:
        await render(update, context, str(exc) if isinstance(exc, MarketplaceError) else "این آگهی در دسترس شما نیست.")
        return ConversationHandler.END
    context.user_data["edit_ad_id"] = ad_id
    await render(update, context, "بخشی را که می‌خواهید اصلاح کنید انتخاب نمایید:", reply_markup=_edit_keyboard(ad_id))
    return EDIT_FIELD


def _edit_keyboard(ad_id: int) -> InlineKeyboardMarkup:
    labels = (("title", "عنوان"), ("description", "تشریح"), ("price", "قیمت"), ("city", "محل"), ("contact", "راه تماس"))
    buttons = [[InlineKeyboardButton(label, callback_data=f"editfield:{ad_id}:{field}")] for field, label in labels]
    buttons.append([InlineKeyboardButton("پایان اصلاح و بازبینی", callback_data="editdone")])
    return InlineKeyboardMarkup(buttons)


async def _edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        _, raw_id, field = query.data.split(":")
        ad_id = int(raw_id)
        if field not in {"title", "description", "price", "city", "contact"}:
            raise ValueError
        ad = _market(context).owned_ad(ad_id, query.from_user.id)
        if not ad or ad["status"] != "draft":
            raise MarketplaceError("این پیش‌نویس در دسترس شما نیست.")
    except (ValueError, MarketplaceError):
        await render(update, context, "این گزینه معتبر نیست یا پیش‌نویس در دسترس نیست.")
        return ConversationHandler.END
    context.user_data["edit_ad_id"] = ad_id
    context.user_data["edit_field"] = field
    prompt = {"title": "عنوان تازه (حداکثر ۱۲۰ نویسه) را بنویسید.", "description": "تشریح تازه (حداکثر ۳۰۰۰ نویسه) را بنویسید.", "price": "قیمت تازه را به افغانی بنویسید؛ نمونه: «۳۰۰۰ افغانی»، «۳ هزار»، «با جورآمد» یا «توافقی».", "city": "محل تازه را بنویسید.", "contact": "راه تماس تازه‌ای را که برای عموم نشر می‌شود بنویسید."}[field]
    await render(update, context, prompt, reply_markup=_wizard_buttons("home"))
    return EDIT_VALUE


@serialized("market")
async def _edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ad_id = context.user_data.get("edit_ad_id")
    field = context.user_data.get("edit_field")
    if not ad_id or not field or not _market(context).owned_ad(ad_id, update.effective_user.id):
        await render(update, context, "روند ویرایش پیدا نشد. از /myads دوباره آغاز کنید.")
        return ConversationHandler.END
    value = update.effective_message.text.strip()
    try:
        if field == "price":
            amount, negotiable = parse_price_input(value)
            _market(context).update_draft_field(ad_id, update.effective_user.id, "price_minor", amount)
            _market(context).update_draft_field(ad_id, update.effective_user.id, "price_negotiable", int(negotiable))
        else:
            if (field == "title" and len(value) > 120) or (field == "description" and len(value) > 3000):
                raise MarketplaceError("متن از حد مجاز درازتر است.")
            _market(context).update_draft_field(ad_id, update.effective_user.id, field, value)
    except MarketplaceError as exc:
        await render(update, context, str(exc), reply_markup=_wizard_buttons("home"))
        return EDIT_VALUE
    await render(update, context, "تغییر ذخیره شد. بخش دیگری را انتخاب کنید یا بازبینی نهایی را بزنید.", reply_markup=_edit_keyboard(ad_id))
    return EDIT_FIELD


async def _edit_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return await _show_confirmation(update, context, query=True)


@serialized("market")
async def _submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = _current_draft(update, context)
    if not draft:
        await render(update, context, "پیش‌نویس پیدا نشد. از منوی اصلی دوباره آغاز کنید.")
        return ConversationHandler.END
    try:
        _market(context).submit(draft["ad_id"], update.effective_user.id)
    except MarketplaceError as exc:
        await render(update, context, str(exc))
        return ConversationHandler.END
    await render(update, context,
        f"آگهی شمارهٔ {draft['ad_id']} برای بررسی مدیر فرستاده شد. تا تأیید مدیر در گروه نشر نمی‌شود.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")]]),
    )
    from handlers.admin import notify_ad_reviewers

    await notify_ad_reviewers(context, draft["ad_id"])
    context.user_data.pop("draft_id", None)
    return ConversationHandler.END


@serialized("market")
async def _cancel_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = _current_draft(update, context)
    if draft:
        try:
            _market(context).transition(draft["ad_id"], "archived")
        except MarketplaceError as exc:
            await render(update, context, f"لغو پیش‌نویس انجام نشد: {exc}")
            return ConversationHandler.END
    context.user_data.pop("draft_id", None)
    context.user_data.pop("ad_category", None)
    text, keyboard = _main_menu(context, update)
    await render(update, context, "روند ثبت آگهی لغو شد.\n\n" + text, reply_markup=keyboard)
    return ConversationHandler.END


@serialized("market")
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = _current_draft(update, context)
    if draft:
        try:
            _market(context).transition(draft["ad_id"], "archived")
        except MarketplaceError as exc:
            await render(update, context, f"لغو پیش‌نویس انجام نشد: {exc}")
            return ConversationHandler.END
    context.user_data.pop("draft_id", None)
    context.user_data.pop("ad_category", None)
    text, keyboard = _main_menu(context, update)
    await render(update, context, "روند لغو شد.\n\n" + text, reply_markup=keyboard)
    return ConversationHandler.END


async def _wizard_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    if chat:
        await context.bot.send_message(
            chat.id,
            "روند ثبت پس از ۳۰ دقیقه بی‌فعالی پایان یافت؛ پیش‌نویس شما حذف نشده است. "
            "برای ادامه، دکمه را بزنید یا /newad را بنویسید.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 ادامهٔ ثبت آگهی", callback_data="newad")]]),
        )
    return ConversationHandler.END


async def my_ads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _touch(update, context)
    await _my_ads_page(update, context, 0)


async def _my_ads_page(update, context, page):
    page = max(0, min(page, 1000000))
    ads = _market(context).user_ads(update.effective_user.id, limit=6, offset=page * 5)
    if not ads and page:
        return await _my_ads_page(update, context, 0)
    text = f"آگهی‌های من | صفحهٔ {page + 1}" if ads else "هنوز آگهی ثبت نکرده‌اید."
    buttons = [
        [InlineKeyboardButton(
            f"#{ad['ad_id']} | {(ad['title'] or 'پیش‌نویس')[:45]} | {_ad_status_label(ad)}",
            callback_data=f"own:detail:{ad['ad_id']}:{page}",
        )]
        for ad in ads[:5]
    ]
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton("قبلی", callback_data=f"own:list:{page - 1}"))
    if len(ads) > 5:
        navigation.append(InlineKeyboardButton("بعدی", callback_data=f"own:list:{page + 1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")])
    await render(update, context, text, reply_markup=InlineKeyboardMarkup(buttons))


async def _my_ad_detail(update, context, ad_id, page):
    ad = _market(context).owned_ad(ad_id, update.effective_user.id)
    if not ad:
        await render(update, context, "آگهی در دسترس شما نیست.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("بازگشت به آگهی‌ها", callback_data=f"own:list:{page}")]]
        ))
        return
    price = format_price(ad) if ad["price_minor"] is not None or ad["price_negotiable"] else "ثبت نشده"
    text = (
        f"آگهی #{ad_id} | {_ad_status_label(ad)}\n{ad['title'] or 'پیش‌نویس'}\n\n"
        f"{ad['description'] or ''}\n\nقیمت: {price}\n"
        f"شهر: {ad['city'] or 'ثبت نشده'}\nتماس: {ad['contact'] or 'ثبت نشده'}"
    )
    buttons = []
    if ad["status"] == "published":
        buttons.append([
            InlineKeyboardButton("فروخته شد", callback_data=f"owner:sold:{ad_id}"),
            InlineKeyboardButton("برداشتن آگهی", callback_data=f"owner:archived:{ad_id}"),
        ])
    if ad["status"] in {"draft", "rejected", "expired", "archived"}:
        buttons.append([InlineKeyboardButton("ویرایش آگهی", callback_data=f"owner:edit:{ad_id}")])
    if ad["status"] in {"rejected", "expired", "archived"}:
        buttons.append([InlineKeyboardButton("تمدید و ثبت دوباره", callback_data=f"owner:renew:{ad_id}")])
    buttons.append([InlineKeyboardButton("⬅️ آگهی‌های من", callback_data=f"own:list:{page}")])
    buttons.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")])
    await render(update, context, text, reply_markup=InlineKeyboardMarkup(buttons))


async def my_ads_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if parts[1] == "list":
        await _my_ads_page(update, context, int(parts[2]))
    else:
        await _my_ad_detail(update, context, int(parts[2]), int(parts[3]))


@serialized("market")
async def owner_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, action, raw_id = query.data.split(":", 2)
        ad_id = int(raw_id)
        if action == "renew":
            _market(context).reopen(ad_id, query.from_user.id)
            await render(update, context, "پیش‌نویس دوباره باز شد. /newad را بزنید تا تکمیل و ثبت شود.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ آگهی‌های من", callback_data="own:list:0")]]))
        else:
            _market(context).owner_action(ad_id, query.from_user.id, action)
            await render(update, context, "وضعیت آگهی به‌روز شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ آگهی‌های من", callback_data="own:list:0")]]))
    except (ValueError, MarketplaceError):
        await render(update, context, "این گزینه معتبر نیست یا آگهی دیگر در دسترس شما نیست.")


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _touch(update, context)
    keyword = " ".join(context.args).strip()[:80]
    session_id = _market(context).create_search_session(update.effective_user.id, keyword)
    await _render_search(update, context, session_id)


def _search_keyboard(session_id: str, search_row, has_next: bool, page: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("واژه", callback_data=f"sr:{session_id}:keyword"), InlineKeyboardButton("شهر", callback_data=f"sr:{session_id}:city")],
        [InlineKeyboardButton("بازهٔ قیمت", callback_data=f"sr:{session_id}:price"), InlineKeyboardButton("دسته‌بندی", callback_data=f"sr:{session_id}:category")],
        [InlineKeyboardButton("نوع آگهی", callback_data=f"sr:{session_id}:type"), InlineKeyboardButton("پاک‌کردن فیلترها", callback_data=f"sr:{session_id}:clear")],
    ]
    if page > 0 or has_next:
        buttons.append([
            *([InlineKeyboardButton("قبلی", callback_data=f"sr:{session_id}:page:{page - 1}")] if page > 0 else []),
            *([InlineKeyboardButton("بعدی", callback_data=f"sr:{session_id}:page:{page + 1}")] if has_next else []),
        ])
    buttons.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


async def _render_search(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: str, page: int = 0, edit: bool = False) -> None:
    market = _market(context)
    owner_id = update.effective_user.id
    session = market.get_search_session(session_id, owner_id)
    if not session:
        await render(update, context, "این نشست جست‌وجو پایان یافته است. /search را دوباره بزنید.")
        return
    total = market.search_count(keyword=session["keyword"], category_id=session["category_id"], city=session["city"],
                                ad_type=session["ad_type"], min_price=session["min_price"], max_price=session["max_price"])
    page = min(max(page, 0), max(0, (total - 1) // 5))
    rows = market.search(keyword=session["keyword"], category_id=session["category_id"], city=session["city"],
                         ad_type=session["ad_type"], min_price=session["min_price"], max_price=session["max_price"],
                         page=page, page_size=6)
    shown = rows[:5]
    filters = []
    if session["keyword"]: filters.append(f"واژه: {html.escape(session['keyword'])}")
    if session["city"]: filters.append(f"شهر: {html.escape(session['city'])}")
    if session["category_id"]:
        category = next((row["name"] for row in market.categories(False) if row["category_id"] == session["category_id"]), "نامعلوم")
        filters.append(f"دسته: {html.escape(category)}")
    if session["ad_type"]: filters.append(f"نوع: {TYPE_LABELS[session['ad_type']]}")
    if session["min_price"] is not None or session["max_price"] is not None:
        filters.append(f"قیمت: {session['min_price'] or '۰'} تا {session['max_price'] or 'باز'} افغانی")
    output = [f"نتیجه‌های جست‌وجو: {total} | صفحهٔ {page + 1}", "فیلترها: " + ("؛ ".join(filters) if filters else "هیچ")]
    for row in shown:
        price = format_price(row) if row["price_minor"] is not None or row["price_negotiable"] else "قیمت ثبت نشده"
        output.append(f"\nآگهی {row['ad_id']} | {html.escape(row['title'])}\n{html.escape(row['category_name'])} | {TYPE_LABELS[row['ad_type']]}\n{price} | {html.escape(row['city'])}\n{html.escape(row['description'][:500])}\nتماس: {html.escape(row['contact'])}")
    if not shown:
        output.append("\nآگهی نشرشده‌ای با این فیلترها پیدا نشد.")
    keyboard = _search_keyboard(session_id, session, len(rows) > 5, page)
    if edit and update.callback_query:
        await render(update, context, "\n".join(output), reply_markup=keyboard, parse_mode="HTML")
    else:
        await render(update, context, "\n".join(output), reply_markup=keyboard, parse_mode="HTML")


async def search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 3:
        await render(update, context, "این گزینه معتبر نیست.")
        return
    kind, session_id, action = parts[:3]
    market = _market(context)
    session = market.get_search_session(session_id, query.from_user.id)
    if not session:
        await render(update, context, "این جست‌وجو برای شما نیست یا پایان یافته است.")
        return
    if kind == "sc":
        try:
            category_id = int(action)
            enabled = {row["category_id"] for row in market.categories()}
            if category_id and category_id not in enabled:
                raise ValueError
            market.update_search_session(session_id, query.from_user.id, "category_id", category_id or None)
        except (ValueError, MarketplaceError):
            await render(update, context, "دسته‌بندی معتبر نیست یا دیگر فعال نیست.")
            return
        await _render_search(update, context, session_id, edit=True)
        return
    if action == "page" and len(parts) == 4:
        try: page = int(parts[3])
        except ValueError: await render(update, context, "شمارهٔ صفحه معتبر نیست."); return
        await _render_search(update, context, session_id, page, edit=True)
    elif action in {"keyword", "city", "price"}:
        market.update_search_session(session_id, query.from_user.id, "pending_field", action)
        prompt = {"keyword": "واژهٔ جست‌وجو را بنویسید (حداکثر ۸۰ نویسه).", "city": "نام شهر را بنویسید.", "price": "بازهٔ قیمت را به شکل حداقل-حداکثر بنویسید؛ برای کرانهٔ باز یکی از عددها را خالی بگذارید."}[action]
        await render(update, context, prompt)
    elif action == "category":
        buttons = [[InlineKeyboardButton("همهٔ دسته‌ها", callback_data=f"sc:{session_id}:0")]]
        buttons.extend([ [InlineKeyboardButton(row["name"], callback_data=f"sc:{session_id}:{row['category_id']}")] for row in market.categories() ])
        buttons.append([InlineKeyboardButton("⬅️ بازگشت به جست‌وجو", callback_data=f"sr:{session_id}:back")])
        await render(update, context, "دسته‌بندی را برگزینید:", reply_markup=InlineKeyboardMarkup(buttons))
    elif action == "type":
        buttons = [[InlineKeyboardButton(label, callback_data=f"sr:{session_id}:settype:{key}")] for key, label in TYPE_LABELS.items()]
        buttons.append([InlineKeyboardButton("همهٔ نوع‌ها", callback_data=f"sr:{session_id}:settype:all")])
        buttons.append([InlineKeyboardButton("⬅️ بازگشت به جست‌وجو", callback_data=f"sr:{session_id}:back")])
        await render(update, context, "نوع آگهی را برگزینید:", reply_markup=InlineKeyboardMarkup(buttons))
    elif action == "settype" and len(parts) == 4:
        ad_type = parts[3]
        if ad_type != "all" and ad_type not in TYPE_LABELS:
            await render(update, context, "نوع آگهی معتبر نیست.")
            return
        market.update_search_session(session_id, query.from_user.id, "ad_type", None if ad_type == "all" else ad_type)
        await _render_search(update, context, session_id, edit=True)
    elif action == "clear":
        for field, value in (("keyword", ""), ("category_id", None), ("city", ""), ("ad_type", None), ("min_price", None), ("max_price", None), ("pending_field", None)):
            market.update_search_session(session_id, query.from_user.id, field, value)
        await _render_search(update, context, session_id, edit=True)
    elif action == "back":
        await _render_search(update, context, session_id, edit=True)
    else:
        await render(update, context, "این گزینه معتبر نیست.")


async def search_filter_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _market(context).pending_search_input(update.effective_user.id)
    if not session:
        return
    market = _market(context)
    value = update.effective_message.text.strip()
    field = session["pending_field"]
    try:
        if value.casefold() in {"لغو", "cancel"}:
            market.update_search_session(session["session_id"], update.effective_user.id, "pending_field", None)
            await render(update, context, "تغییر فیلتر لغو شد.")
            return
        if field in {"keyword", "city"}:
            if not value or len(value) > 80:
                raise MarketplaceError("متن خالی یا بیش از اندازه دراز است.")
            market.update_search_session(session["session_id"], update.effective_user.id, field, value)
        else:
            digits = value.translate(PERSIAN_DIGITS)
            pieces = re.split(r"\s*[-–]\s*", digits, maxsplit=1)
            if len(pieces) != 2 or not any(pieces):
                raise MarketplaceError("قیمت را به شکل حداقل-حداکثر بنویسید؛ نمونه: ۰-۵۰۰۰۰.")
            bounds = [int(re.sub(r"[,،٬\s]", "", part)) if part else None for part in pieces]
            if any(bound is not None and (bound < 0 or bound > 10**14) for bound in bounds) or (bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]):
                raise MarketplaceError("بازهٔ قیمت معتبر نیست.")
            market.update_search_session(session["session_id"], update.effective_user.id, "min_price", bounds[0])
            market.update_search_session(session["session_id"], update.effective_user.id, "max_price", bounds[1])
        market.update_search_session(session["session_id"], update.effective_user.id, "pending_field", None)
    except (MarketplaceError, ValueError) as exc:
        await render(update, context, str(exc) if isinstance(exc, MarketplaceError) else "قیمت باید عدد معتبر باشد.")
        return
    await _render_search(update, context, session["session_id"])


async def report_ad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        ad_id = int(query.data.split(":", 1)[1])
        _market(context).ensure_user(query.from_user.id, query.from_user.username, query.from_user.full_name)
        _market(context).add_report(ad_id, query.from_user.id, "suspected_violation")
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("گزارش شما به مدیران رسید. سپاس از همکاری‌تان.")
    except (ValueError, MarketplaceError) as exc:
        await query.message.reply_text(str(exc) if isinstance(exc, MarketplaceError) else "شناسهٔ آگهی معتبر نیست.")


async def navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data in {"search", "menu:search"}:
        session_id = _market(context).create_search_session(query.from_user.id)
        await _render_search(update, context, session_id, edit=True)
    elif query.data == "menu:home":
        text, keyboard = _main_menu(context, update)
        await render(update, context, text, reply_markup=keyboard)
    elif query.data == "menu:rules":
        text = effective_rules(context)
        await render(update, context,
            "📋 مقررات دوکانچه:\n" + text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")]]),
        )
    elif query.data == "menu:help":
        await render(update, context,
            "💬 کمک و همکاری\nبرای پشتیبانی یا همکاری با خلیل نوری تماس بگیرید.\nبرای فعالیت در گروه، وضعیت دعوت‌ها را از دکمهٔ زیر ببینید.",
            reply_markup=help_keyboard(),
        )
    elif query.data == "menu:categories":
        buttons = [[InlineKeyboardButton(row["name"], callback_data=f"browse:{row['category_id']}")] for row in _market(context).categories()]
        buttons.append([InlineKeyboardButton("همهٔ آگهی‌ها", callback_data="browse:all")])
        buttons.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")])
        await render(update, context, "📂 یک دسته‌بندی را برای دیدن آگهی‌ها برگزینید:", reply_markup=InlineKeyboardMarkup(buttons))
    elif query.data.startswith("browse:"):
        selection = query.data.partition(":")[2]
        try:
            category_id = None if selection == "all" else int(selection)
        except ValueError:
            await render(update, context, "دسته‌بندی معتبر نیست.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")]]))
            return
        session_id = _market(context).create_search_session(query.from_user.id)
        if category_id is not None:
            enabled = {row["category_id"] for row in _market(context).categories()}
            if category_id not in enabled:
                await render(update, context, "این دسته‌بندی دیگر فعال نیست.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:home")]]))
                return
            _market(context).update_search_session(session_id, query.from_user.id, "category_id", category_id)
        await _render_search(update, context, session_id, edit=True)


async def wizard_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    destination = query.data.partition(":back:")[2]
    if destination == "home":
        text, keyboard = _main_menu(context, update)
        await render(update, context, text, reply_markup=keyboard)
        return ConversationHandler.END

    labels = {
        "category": (CATEGORY, "دسته‌بندی آگهی را انتخاب کنید:"),
        "type": (AD_TYPE, "نوع آگهی را انتخاب کنید:"),
        "title": (TITLE, "عنوان آگهی را بنویسید؛ تا ۱۲۰ نویسه."),
        "description": (DESCRIPTION, "تشریح آگهی را بنویسید؛ تا ۳۰۰۰ نویسه."),
        "price": (PRICE, "قیمت را به افغانی بنویسید؛ نمونه: «۳۰۰۰ افغانی» یا «۳ هزار»، یا «با جورآمد» / «توافقی»."),
        "city": (CITY, "شهر یا محل آگهی را بنویسید."),
        "contact": (CONTACT, "راه تماس را بنویسید. این معلومات در آگهی عمومی نشر می‌شود."),
        "photos": (PHOTOS, f"عکس‌های آگهی را بفرستید؛ تاکنون {len(_market(context).photos(_current_draft(update, context)['ad_id'])) if _current_draft(update, context) else 0} عکس ثبت شده است."),
    }
    if destination not in labels:
        await render(update, context, "این مرحله پیدا نشد. روند ثبت را لغو و دوباره آغاز کنید.")
        return ConversationHandler.END
    state, prompt = labels[destination]
    if destination == "category":
        rows = _market(context).categories()
        buttons = [[InlineKeyboardButton(f"{category_emoji(row['name'])} {row['name']}", callback_data=f"adcat:{row['category_id']}")] for row in rows]
        buttons.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="ad:back:home")])
        markup = InlineKeyboardMarkup(buttons)
    elif destination == "type":
        buttons = [[InlineKeyboardButton(label, callback_data=f"adtype:{key}")] for key, label in TYPE_LABELS.items()]
        buttons.append(_wizard_buttons("category").inline_keyboard[0])
        markup = InlineKeyboardMarkup(buttons)
    elif destination == "photos":
        buttons = [[InlineKeyboardButton("پایان عکس‌ها", callback_data="photos-done")], *_wizard_buttons("contact").inline_keyboard]
        markup = InlineKeyboardMarkup(buttons)
    else:
        back_to = {"title": "type", "description": "title", "price": "description", "city": "price", "contact": "city"}[destination]
        markup = _wizard_buttons(back_to)
    await render(update, context, prompt, reply_markup=markup)
    return state


async def _wizard_start(update, context):
    await start(update, context)
    return ConversationHandler.END


async def _wizard_admin(update, context):
    from handlers.admin import admin_panel
    await admin_panel(update, context)
    return ConversationHandler.END


async def _wizard_home(update, context):
    await navigation(update, context)
    return ConversationHandler.END


async def _wizard_menu_command(update, context):
    command = update.effective_message.text.split(maxsplit=1)[0].split("@")[0]
    if command == "/invites":
        from handlers.group_gate import invite_status
        await invite_status(update, context)
    else:
        await {
            "/help": help_command, "/rules": rules, "/myads": my_ads,
            "/search": search, "/rankings": rankings,
        }[command](update, context)
    return ConversationHandler.END


def user_handlers() -> list:
    wizard = ConversationHandler(
        entry_points=[
            CommandHandler("newad", _begin),
            CallbackQueryHandler(_begin, pattern=r"^newad$"),
            CallbackQueryHandler(_edit_start, pattern=r"^owner:edit:\d+$"),
        ],
        states={
            CATEGORY: [CallbackQueryHandler(_select_category, pattern=r"^adcat:\d+$")],
            AD_TYPE: [CallbackQueryHandler(_select_type, pattern=r"^adtype:(sale|wanted|rental|service)$")],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _title)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, _description)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _price)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, _city)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _contact)],
            PHOTOS: [MessageHandler(filters.PHOTO, _photo), CallbackQueryHandler(_photos_done, pattern=r"^photos-done$")],
            CONFIRM: [
                CallbackQueryHandler(_submit, pattern=r"^ad-submit$"),
                CallbackQueryHandler(_edit_start, pattern=r"^owner:edit:\d+$"),
                CallbackQueryHandler(_cancel_draft, pattern=r"^ad-cancel$"),
            ],
            EDIT_FIELD: [CallbackQueryHandler(_edit_field, pattern=r"^editfield:\d+:(title|description|price|city|contact)$"), CallbackQueryHandler(_edit_done, pattern=r"^editdone$")],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _edit_value)],
            ConversationHandler.TIMEOUT: [TypeHandler(Update, _wizard_timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", _wizard_start),
            CommandHandler("admin", _wizard_admin),
            CommandHandler(["help", "rules", "myads", "search", "rankings", "invites"], _wizard_menu_command),
            CallbackQueryHandler(_wizard_home, pattern=r"^menu:home$"),
            CallbackQueryHandler(_cancel_draft, pattern=r"^ad:cancel$"),
            CallbackQueryHandler(wizard_navigation, pattern=r"^ad:back:[a-z]+$"),
        ],
        allow_reentry=True,
        conversation_timeout=30 * 60,
        # The wizard intentionally spans callback messages and subsequent text updates for one user/chat.
        per_message=False,
    )
    return [
        wizard,
        CommandHandler("start", start), CommandHandler("help", help_command), CommandHandler("rules", rules),
        CommandHandler("myads", my_ads), CommandHandler("search", search), CommandHandler("rankings", rankings),
        CommandHandler("cancel", cancel),
        MessageHandler(filters.TEXT & ~filters.COMMAND, search_filter_text),
        CallbackQueryHandler(search_callback, pattern=r"^(sr|sc):"),
        CallbackQueryHandler(my_ads_callback, pattern=r"^own:(?:list:\d+|detail:\d+:\d+)$"),
        CallbackQueryHandler(owner_action, pattern=r"^owner:(sold|archived|renew):\d+$"),
        CallbackQueryHandler(report_ad, pattern=r"^report:\d+$"),
        CallbackQueryHandler(navigation, pattern=r"^(search|menu:(home|rules|help|categories|search)|browse:(all|\d+))$"),
    ]
