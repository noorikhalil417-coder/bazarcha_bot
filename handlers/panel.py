"""Render private menus in one persistent bot message."""

import json

from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

_PANEL_CALLBACK_PREFIXES = (
    "menu:", "browse:", "sr:", "sc:", "gate:", "group:invites",
    "admin:", "setting:", "category:", "trust:", "featured:",
    "owner:", "own:", "newad", "ad:back:", "adcat:", "adtype:",
    "editfield:", "editdone", "photos-done", "ad-submit", "ad-cancel",
)


def _private(update):
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    return chat is not None and chat.type == "private" and user is not None


def _panel(db, chat_id, user_id):
    with db.connect() as conn:
        return conn.execute(
            "SELECT message_id,callbacks FROM private_panels WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()


def _save(db, chat_id, user_id, message_id, markup):
    callbacks = [
        button.callback_data
        for row in (markup.inline_keyboard if markup else ())
        for button in row
        if button.callback_data
    ]
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO private_panel_messages(chat_id,user_id,message_id) VALUES(?,?,?)",
            (chat_id, user_id, message_id),
        )
        conn.execute(
            "INSERT INTO private_panels(chat_id,user_id,message_id,callbacks) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id,"
            "message_id=excluded.message_id,callbacks=excluded.callbacks",
            (chat_id, user_id, message_id, json.dumps(callbacks)),
        )


def _unchanged(exc):
    return str(exc).casefold().startswith("message is not modified")


def _uneditable(exc):
    message = str(exc).casefold()
    return message.startswith(("message to edit not found", "message can't be edited"))


async def render(update, context, text, **kwargs):
    query = getattr(update, "callback_query", None)
    if not _private(update):
        if query:
            return await query.edit_message_text(text, **kwargs)
        return await update.effective_message.reply_text(text, **kwargs)

    db = context.application.bot_data["database"]
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    panel = _panel(db, chat_id, user_id)
    if query and (not panel or query.message.message_id != panel["message_id"]):
        return await query.edit_message_text(text, **kwargs)

    if panel:
        try:
            if query:
                result = await query.edit_message_text(text, **kwargs)
            else:
                result = await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=panel["message_id"], text=text, **kwargs
                )
        except BadRequest as exc:
            if _unchanged(exc):
                result = None
            elif _uneditable(exc):
                panel = None
            else:
                raise
        if panel:
            _save(db, chat_id, user_id, panel["message_id"], kwargs.get("reply_markup"))
            return result

    result = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    _save(db, chat_id, user_id, result.message_id, kwargs.get("reply_markup"))
    return result


async def reject_stale_panel_callback(update, context):
    if not _private(update) or not update.callback_query:
        return
    query = update.callback_query
    panel = _panel(context.application.bot_data["database"], update.effective_chat.id, update.effective_user.id)
    if not panel:
        return
    current = query.message.message_id == panel["message_id"]
    with context.application.bot_data["database"].connect() as conn:
        previous = conn.execute(
            "SELECT 1 FROM private_panel_messages WHERE chat_id=? AND user_id=? AND message_id=?",
            (update.effective_chat.id, update.effective_user.id, query.message.message_id),
        ).fetchone()
    stale = (
        query.data not in json.loads(panel["callbacks"]) if current
        else bool(previous) or isinstance(query.data, str) and query.data.startswith(_PANEL_CALLBACK_PREFIXES)
    )
    if stale:
        await query.answer("این گزینه دیگر فعال نیست.")
        raise ApplicationHandlerStop
