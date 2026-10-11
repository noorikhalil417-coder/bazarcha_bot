"""Dari user commands and persistent advertisement submission flow."""

from __future__ import annotations

import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from services.marketplace import Marketplace, MarketplaceError

CATEGORY, AD_TYPE, TITLE, DESCRIPTION, PRICE, CITY, CONTACT, PHOTOS, CONFIRM = range(9)
EDIT_FIELD, EDIT_VALUE = range(9, 11)
TYPE_LABELS = {"sale": "فروش", "wanted": "خرید", "rental": "کرایه", "service": "خدمات"}
STATUS_LABELS = {"draft": "پیش‌نویس", "pending": "در انتظار بررسی", "approved": "تأییدشده", "published": "نشرشده", "rejected": "نیازمند اصلاح", "expired": "پایان‌یافته", "archived": "بایگانی‌شده", "sold": "فروخته‌شده", "publication_failed": "مشکل در نشر"}
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _market(context: ContextTypes.DEFAULT_TYPE) -> Marketplace:
    return context.application.bot_data["marketplace"]


def _touch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        _market(context).ensure_user(user.id, user.username, user.full_name)


def _current_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return _market(context).draft_for_user(update.effective_user.id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _touch(update, context)
    await update.effective_message.reply_text(
        "به بازارچه خوش آمدید! از این‌جا می‌توانید آگهی ثبت کنید یا آگهی‌های نشرشده را جست‌وجو نمایید.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ثبت آگهی", callback_data="newad")], [InlineKeyboardButton("جست‌وجوی آگهی", callback_data="search")]]),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("فرمان‌ها:\n/newad ثبت آگهی\n/myads آگهی‌های من\n/search واژه جست‌وجو با فیلترها\n/rankings فعالیت فروشندگان در هفت روز گذشته\n/rules دیدن مقررات\n/cancel لغو روند جاری\nبرای کمک بیشتر با مدیران گروه تماس بگیرید.")


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _market(context).get_setting("rules", context.application.bot_data["config"].group_rules)
    await update.effective_message.reply_text("مقررات بازارچه:\n" + text)


async def rankings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    market = _market(context)
    if market.get_setting("public_rankings", "1") != "1":
        await update.effective_message.reply_text("نمایش همگانی رتبه‌بندی از سوی مدیران خاموش شده است.")
        return
    rows = market.seller_rankings()
    if not rows:
        await update.effective_message.reply_text("در هفت روز گذشته امتیاز تأییدشده‌ای برای رتبه‌بندی ثبت نشده است.")
        return
    lines = ["فعالیت فروشندگان در هفت روز گذشته (امتیاز، گواه اعتماد نیست):"]
    lines.extend(f"{index}. {html.escape(row['display_name'] or row['username'] or 'فروشنده')} | {row['score']} امتیاز | {row['approved_ads']} آگهی تأییدشده" for index, row in enumerate(rows, 1))
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def _begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _touch(update, context)
    draft = _current_draft(update, context)
    if draft:
        context.user_data["draft_id"] = draft["ad_id"]
        if not draft["title"]:
            await update.effective_message.reply_text("پیش‌نویس قبلی ادامه می‌یابد. عنوان آگهی را بنویسید.")
            return TITLE
        if not draft["description"]:
            await update.effective_message.reply_text("پیش‌نویس قبلی ادامه می‌یابد. تشریح آگهی را بنویسید.")
            return DESCRIPTION
        if draft["price_minor"] is None and not draft["price_negotiable"]:
            await update.effective_message.reply_text("پیش‌نویس قبلی ادامه می‌یابد. قیمت را به افغانی بنویسید یا «توافقی».")
            return PRICE
        if not draft["city"]:
            await update.effective_message.reply_text("پیش‌نویس قبلی ادامه می‌یابد. شهر یا محل را بنویسید.")
            return CITY
        if not draft["contact"]:
            await update.effective_message.reply_text("پیش‌نویس قبلی ادامه می‌یابد. راه تماس را بنویسید.")
            return CONTACT
        return await _show_confirmation(update, context)
    categories = _market(context).categories()
    buttons = [[InlineKeyboardButton(row["name"], callback_data=f"adcat:{row['category_id']}")] for row in categories]
    await update.effective_message.reply_text("دسته‌بندی آگهی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))
    return CATEGORY


async def _select_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        context.user_data["ad_category"] = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("دسته‌بندی معتبر نیست. دوباره /newad را بزنید.")
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(label, callback_data=f"adtype:{key}")] for key, label in TYPE_LABELS.items()]
    await query.edit_message_text("نوع آگهی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))
    return AD_TYPE


async def _select_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    kind = query.data.partition(":")[2]
    if kind not in TYPE_LABELS:
        await query.edit_message_text("نوع آگهی معتبر نیست. دوباره /newad را بزنید.")
        return ConversationHandler.END
    try:
        ad_id = _market(context).create_draft(update.effective_user.id, context.user_data.pop("ad_category"), kind)
    except (MarketplaceError, KeyError) as exc:
        await query.edit_message_text(str(exc) if isinstance(exc, MarketplaceError) else "روند ثبت آگهی را دوباره آغاز کنید.")
        return ConversationHandler.END
    context.user_data["draft_id"] = ad_id
    await query.edit_message_text("عنوان آگهی را بنویسید؛ تا ۱۲۰ نویسه.")
    return TITLE


async def _save_text(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, next_state: int, prompt: str) -> int:
    _touch(update, context)
    draft = _current_draft(update, context)
    if not draft:
        await update.effective_message.reply_text("پیش‌نویس پیدا نشد. لطفاً /newad را دوباره بزنید.")
        return ConversationHandler.END
    try:
        _market(context).update_draft_field(draft["ad_id"], update.effective_user.id, field, update.effective_message.text)
    except MarketplaceError as exc:
        await update.effective_message.reply_text(str(exc))
        return {"title": TITLE, "description": DESCRIPTION, "city": CITY, "contact": CONTACT}[field]
    await update.effective_message.reply_text(prompt)
    return next_state


async def _title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if len(update.effective_message.text.strip()) > 120:
        await update.effective_message.reply_text("عنوان باید حداکثر ۱۲۰ نویسه باشد. کوتاه‌تر بنویسید.")
        return TITLE
    return await _save_text(update, context, "title", DESCRIPTION, "تشریح آگهی را بنویسید؛ تا ۳۰۰۰ نویسه.")


async def _description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if len(update.effective_message.text.strip()) > 3000:
        await update.effective_message.reply_text("تشریح باید حداکثر ۳۰۰۰ نویسه باشد.")
        return DESCRIPTION
    return await _save_text(update, context, "description", PRICE, "قیمت را به افغانی بنویسید؛ برای قیمت توافقی واژهٔ «توافقی» را بفرستید.")


async def _price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = _current_draft(update, context)
    if not draft:
        await update.effective_message.reply_text("پیش‌نویس پیدا نشد. /newad را دوباره بزنید.")
        return ConversationHandler.END
    value = update.effective_message.text.strip()
    if value.casefold() in {"توافقی", "قابل مذاکره"}:
        _market(context).update_draft_field(draft["ad_id"], update.effective_user.id, "price_negotiable", 1)
        _market(context).update_draft_field(draft["ad_id"], update.effective_user.id, "price_minor", None)
    else:
        digits = re.sub(r"[,،٬\s]", "", value.translate(PERSIAN_DIGITS))
        if not digits.isdecimal() or int(digits) <= 0 or int(digits) > 10**12:
            await update.effective_message.reply_text("قیمت معتبر نیست. یک عدد مثبت یا «توافقی» بنویسید.")
            return PRICE
        _market(context).update_draft_field(draft["ad_id"], update.effective_user.id, "price_minor", int(digits))
        _market(context).update_draft_field(draft["ad_id"], update.effective_user.id, "price_negotiable", 0)
    await update.effective_message.reply_text("شهر یا محل آگهی را بنویسید.")
    return CITY


async def _city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _save_text(update, context, "city", CONTACT, "راه تماس مورد تأیید برای نشر عمومی را بنویسید.")


async def _contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    result = await _save_text(update, context, "contact", PHOTOS, "عکس‌های آگهی را بفرستید (حداکثر ۱۰ عکس). اگر عکس ندارید، «پایان عکس‌ها» را بزنید.")
    if result == PHOTOS:
        await update.effective_message.reply_text("عکس‌ها اختیاری است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("پایان عکس‌ها", callback_data="photos-done")]]))
    return result


async def _photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = _current_draft(update, context)
    if not draft:
        await update.effective_message.reply_text("پیش‌نویس پیدا نشد؛ /newad را دوباره بزنید.")
        return ConversationHandler.END
    try:
        _market(context).add_photo(draft["ad_id"], update.effective_user.id, update.effective_message.photo[-1].file_id)
    except MarketplaceError as exc:
        await update.effective_message.reply_text(str(exc))
    else:
        await update.effective_message.reply_text("عکس افزوده شد. عکس دیگر بفرستید یا «پایان عکس‌ها» را بزنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("پایان عکس‌ها", callback_data="photos-done")]]))
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
        price = "توافقی" if draft["price_negotiable"] else f"{draft['price_minor']:,} افغانی"
        text = f"پیش‌نمایش آگهی:\n{html.escape(draft['title'])}\nدسته: {html.escape(category)} | نوع: {TYPE_LABELS[draft['ad_type']]}\nقیمت: {price}\nمحل: {html.escape(draft['city'])}\nتشریح: {html.escape(draft['description'])}\nتماس (به‌صورت عمومی نشر می‌شود): {html.escape(draft['contact'])}\nعکس‌ها: {len(_market(context).photos(draft['ad_id']))}\n\nتأیید می‌کنید؟"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("ثبت برای بررسی", callback_data="ad-submit"), InlineKeyboardButton("لغو", callback_data="ad-cancel")]])
    target = update.callback_query.message if query else update.effective_message
    await target.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return CONFIRM if draft else ConversationHandler.END


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
        await query.edit_message_text(str(exc) if isinstance(exc, MarketplaceError) else "این آگهی در دسترس شما نیست.")
        return ConversationHandler.END
    context.user_data["edit_ad_id"] = ad_id
    await query.edit_message_text("بخشی را که می‌خواهید اصلاح کنید انتخاب نمایید:", reply_markup=_edit_keyboard(ad_id))
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
        await query.edit_message_text("این گزینه معتبر نیست یا پیش‌نویس در دسترس نیست.")
        return ConversationHandler.END
    context.user_data["edit_ad_id"] = ad_id
    context.user_data["edit_field"] = field
    prompt = {"title": "عنوان تازه (حداکثر ۱۲۰ نویسه) را بنویسید.", "description": "تشریح تازه (حداکثر ۳۰۰۰ نویسه) را بنویسید.", "price": "قیمت تازه را به افغانی یا «توافقی» بنویسید.", "city": "محل تازه را بنویسید.", "contact": "راه تماس تازه‌ای را که برای عموم نشر می‌شود بنویسید."}[field]
    await query.message.reply_text(prompt)
    return EDIT_VALUE


async def _edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ad_id = context.user_data.get("edit_ad_id")
    field = context.user_data.get("edit_field")
    if not ad_id or not field or not _market(context).owned_ad(ad_id, update.effective_user.id):
        await update.effective_message.reply_text("روند ویرایش پیدا نشد. از /myads دوباره آغاز کنید.")
        return ConversationHandler.END
    value = update.effective_message.text.strip()
    try:
        if field == "price":
            if value.casefold() in {"توافقی", "قابل مذاکره"}:
                _market(context).update_draft_field(ad_id, update.effective_user.id, "price_minor", None)
                _market(context).update_draft_field(ad_id, update.effective_user.id, "price_negotiable", 1)
            else:
                digits = re.sub(r"[,،٬\s]", "", value.translate(PERSIAN_DIGITS))
                if not digits.isdecimal() or int(digits) <= 0 or int(digits) > 10**12:
                    raise MarketplaceError("قیمت معتبر نیست؛ عدد مثبت یا «توافقی» بنویسید.")
                _market(context).update_draft_field(ad_id, update.effective_user.id, "price_minor", int(digits))
                _market(context).update_draft_field(ad_id, update.effective_user.id, "price_negotiable", 0)
        else:
            if (field == "title" and len(value) > 120) or (field == "description" and len(value) > 3000):
                raise MarketplaceError("متن از حد مجاز درازتر است.")
            _market(context).update_draft_field(ad_id, update.effective_user.id, field, value)
    except MarketplaceError as exc:
        await update.effective_message.reply_text(str(exc))
        return EDIT_VALUE
    await update.effective_message.reply_text("تغییر ذخیره شد. بخش دیگری را انتخاب کنید یا بازبینی نهایی را بزنید.", reply_markup=_edit_keyboard(ad_id))
    return EDIT_FIELD


async def _edit_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return await _show_confirmation(update, context, query=True)


async def _submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = _current_draft(update, context)
    if not draft:
        await query.edit_message_text("پیش‌نویس پیدا نشد. دوباره /newad را بزنید.")
        return ConversationHandler.END
    config = context.application.bot_data["config"]
    review_mode = _market(context).get_setting("review_mode", config.moderation_review_mode)
    try:
        status = _market(context).submit(draft["ad_id"], update.effective_user.id, review_mode == "required")
    except MarketplaceError as exc:
        await query.edit_message_text(str(exc))
        return ConversationHandler.END
    if status == "pending":
        await query.edit_message_text(f"آگهی شمارهٔ {draft['ad_id']} برای بررسی مدیر فرستاده شد.")
        for admin_id in config.admin_ids:
            try:
                await context.bot.send_message(admin_id, f"آگهی تازهٔ شمارهٔ {draft['ad_id']} در انتظار بررسی است. /admin را بزنید.")
            except TelegramError as exc:
                logging.getLogger(__name__).info("Could not notify admin %s: %s", admin_id, type(exc).__name__)
    else:
        await query.edit_message_text(f"آگهی شمارهٔ {draft['ad_id']} ثبت شد و برای نشر آماده است.")
        await context.application.bot_data["publish_ad"](context, draft["ad_id"], 0)
    context.user_data.pop("draft_id", None)
    return ConversationHandler.END


async def _cancel_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = _current_draft(update, context)
    if draft:
        try: _market(context).transition(draft["ad_id"], "archived")
        except MarketplaceError: pass
    await query.edit_message_text("روند ثبت آگهی لغو شد.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = _current_draft(update, context)
    if draft:
        try: _market(context).transition(draft["ad_id"], "archived")
        except MarketplaceError: pass
    await update.effective_message.reply_text("روند لغو شد. برای آغاز دوباره /newad را بزنید.")
    return ConversationHandler.END


async def my_ads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _touch(update, context)
    ads = _market(context).user_ads(update.effective_user.id)
    if not ads:
        await update.effective_message.reply_text("هنوز آگهی ثبت نکرده‌اید. برای آغاز /newad را بزنید.")
        return
    for ad in ads:
        text = f"آگهی {ad['ad_id']} | {STATUS_LABELS.get(ad['status'], ad['status'])}\n{html.escape(ad['title'] or 'پیش‌نویس') }"
        actions = []
        if ad["status"] == "published":
            actions = [[InlineKeyboardButton("فروخته شد", callback_data=f"owner:sold:{ad['ad_id']}"), InlineKeyboardButton("برداشتن آگهی", callback_data=f"owner:archived:{ad['ad_id']}")]]
        if ad["status"] in {"draft", "rejected", "expired", "archived"}:
            actions = [[InlineKeyboardButton("ویرایش آگهی", callback_data=f"owner:edit:{ad['ad_id']}")]]
        if ad["status"] in {"rejected", "expired", "archived"}:
            actions[0].append(InlineKeyboardButton("تمدید و ثبت دوباره", callback_data=f"owner:renew:{ad['ad_id']}"))
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(actions) if actions else None, parse_mode="HTML")


async def owner_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, action, raw_id = query.data.split(":", 2)
        ad_id = int(raw_id)
        if action == "renew":
            _market(context).reopen(ad_id, query.from_user.id)
            await query.edit_message_text("پیش‌نویس دوباره باز شد. /newad را بزنید تا تکمیل و ثبت شود.")
        else:
            _market(context).owner_action(ad_id, query.from_user.id, action)
            await query.edit_message_text("وضعیت آگهی به‌روز شد.")
    except (ValueError, MarketplaceError):
        await query.edit_message_text("این گزینه معتبر نیست یا آگهی دیگر در دسترس شما نیست.")


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
    return InlineKeyboardMarkup(buttons)


async def _render_search(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: str, page: int = 0, edit: bool = False) -> None:
    market = _market(context)
    owner_id = update.effective_user.id
    session = market.get_search_session(session_id, owner_id)
    if not session:
        target = update.callback_query.message if update.callback_query else update.effective_message
        await target.reply_text("این نشست جست‌وجو پایان یافته است. /search را دوباره بزنید.")
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
        price = "توافقی" if row["price_negotiable"] else f"{row['price_minor']:,} افغانی" if row["price_minor"] is not None else "قیمت ثبت نشده"
        output.append(f"\nآگهی {row['ad_id']} | {html.escape(row['title'])}\n{html.escape(row['category_name'])} | {TYPE_LABELS[row['ad_type']]}\n{price} | {html.escape(row['city'])}\n{html.escape(row['description'][:500])}\nتماس: {html.escape(row['contact'])}")
    if not shown:
        output.append("\nآگهی نشرشده‌ای با این فیلترها پیدا نشد.")
    keyboard = _search_keyboard(session_id, session, len(rows) > 5, page)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text("\n".join(output), reply_markup=keyboard, parse_mode="HTML")
    else:
        target = update.callback_query.message if update.callback_query else update.effective_message
        await target.reply_text("\n".join(output), reply_markup=keyboard, parse_mode="HTML")


async def search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 3:
        await query.edit_message_text("این گزینه معتبر نیست.")
        return
    kind, session_id, action = parts[:3]
    market = _market(context)
    session = market.get_search_session(session_id, query.from_user.id)
    if not session:
        await query.edit_message_text("این جست‌وجو برای شما نیست یا پایان یافته است.")
        return
    if kind == "sc":
        try:
            category_id = int(action)
            enabled = {row["category_id"] for row in market.categories()}
            if category_id and category_id not in enabled:
                raise ValueError
            market.update_search_session(session_id, query.from_user.id, "category_id", category_id or None)
        except (ValueError, MarketplaceError):
            await query.edit_message_text("دسته‌بندی معتبر نیست یا دیگر فعال نیست.")
            return
        await _render_search(update, context, session_id, edit=True)
        return
    if action == "page" and len(parts) == 4:
        try: page = int(parts[3])
        except ValueError: await query.edit_message_text("شمارهٔ صفحه معتبر نیست."); return
        await _render_search(update, context, session_id, page, edit=True)
    elif action in {"keyword", "city", "price"}:
        market.update_search_session(session_id, query.from_user.id, "pending_field", action)
        prompt = {"keyword": "واژهٔ جست‌وجو را بنویسید (حداکثر ۸۰ نویسه).", "city": "نام شهر را بنویسید.", "price": "بازهٔ قیمت را به شکل حداقل-حداکثر بنویسید؛ برای کرانهٔ باز یکی از عددها را خالی بگذارید."}[action]
        await query.message.reply_text(prompt)
    elif action == "category":
        buttons = [[InlineKeyboardButton("همهٔ دسته‌ها", callback_data=f"sc:{session_id}:0")]]
        buttons.extend([ [InlineKeyboardButton(row["name"], callback_data=f"sc:{session_id}:{row['category_id']}")] for row in market.categories() ])
        await query.edit_message_text("دسته‌بندی را برگزینید:", reply_markup=InlineKeyboardMarkup(buttons))
    elif action == "type":
        buttons = [[InlineKeyboardButton(label, callback_data=f"sr:{session_id}:settype:{key}")] for key, label in TYPE_LABELS.items()]
        buttons.append([InlineKeyboardButton("همهٔ نوع‌ها", callback_data=f"sr:{session_id}:settype:all")])
        await query.edit_message_text("نوع آگهی را برگزینید:", reply_markup=InlineKeyboardMarkup(buttons))
    elif action == "settype" and len(parts) == 4:
        ad_type = parts[3]
        if ad_type != "all" and ad_type not in TYPE_LABELS:
            await query.edit_message_text("نوع آگهی معتبر نیست.")
            return
        market.update_search_session(session_id, query.from_user.id, "ad_type", None if ad_type == "all" else ad_type)
        await _render_search(update, context, session_id, edit=True)
    elif action == "clear":
        for field, value in (("keyword", ""), ("category_id", None), ("city", ""), ("ad_type", None), ("min_price", None), ("max_price", None), ("pending_field", None)):
            market.update_search_session(session_id, query.from_user.id, field, value)
        await _render_search(update, context, session_id, edit=True)
    else:
        await query.edit_message_text("این گزینه معتبر نیست.")


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
            await update.effective_message.reply_text("تغییر فیلتر لغو شد.")
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
        await update.effective_message.reply_text(str(exc) if isinstance(exc, MarketplaceError) else "قیمت باید عدد معتبر باشد.")
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
    if query.data == "newad":
        await query.message.reply_text("برای ثبت آگهی، فرمان /newad را بزنید.")
    elif query.data == "search":
        session_id = _market(context).create_search_session(query.from_user.id)
        await _render_search(update, context, session_id)


def user_handlers() -> list:
    wizard = ConversationHandler(
        entry_points=[CommandHandler("newad", _begin), CallbackQueryHandler(_edit_start, pattern=r"^owner:edit:\d+$")],
        states={
            CATEGORY: [CallbackQueryHandler(_select_category, pattern=r"^adcat:\d+$")],
            AD_TYPE: [CallbackQueryHandler(_select_type, pattern=r"^adtype:(sale|wanted|rental|service)$")],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _title)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, _description)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _price)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, _city)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _contact)],
            PHOTOS: [MessageHandler(filters.PHOTO, _photo), CallbackQueryHandler(_photos_done, pattern=r"^photos-done$")],
            CONFIRM: [CallbackQueryHandler(_submit, pattern=r"^ad-submit$"), CallbackQueryHandler(_cancel_draft, pattern=r"^ad-cancel$")],
            EDIT_FIELD: [CallbackQueryHandler(_edit_field, pattern=r"^editfield:\d+:(title|description|price|city|contact)$"), CallbackQueryHandler(_edit_done, pattern=r"^editdone$")],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _edit_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
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
        CallbackQueryHandler(owner_action, pattern=r"^owner:(sold|archived|renew):\d+$"),
        CallbackQueryHandler(report_ad, pattern=r"^report:\d+$"),
        CallbackQueryHandler(navigation, pattern=r"^(newad|search)$"),
    ]