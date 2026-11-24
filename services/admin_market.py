"""Web administration of existing marketplace moderation workflows."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aiohttp import web
from telegram.error import TelegramError

from services.marketplace import MarketplaceError
from services.marketplace_web import sync_group

logger = logging.getLogger(__name__)


def register_admin_market_routes(app, application):
    market = application.bot_data["marketplace"]
    db = market.db
    lock = asyncio.Lock()

    def page(request):
        return max(0, min(int(request.query.get("page", "0")), 10000))

    async def ads(request):
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT a.ad_id,a.owner_id,a.title,a.description,a.city,a.contact,a.status,a.featured_until,"
                "a.publication_message_id,c.name category_name,(SELECT count(*) FROM ad_photos p WHERE p.ad_id=a.ad_id) photo_count "
                "FROM ads a JOIN categories c USING(category_id) ORDER BY a.ad_id DESC LIMIT 51 OFFSET ?",
                (page(request) * 50,),
            ).fetchall()
        return web.json_response({"items": [dict(row) for row in rows[:50]], "has_more": len(rows) > 50})

    async def ad_photo(request):
        ad_id, index = int(request.match_info["ad_id"]), int(request.match_info["index"])
        if index < 0 or index >= 10:
            raise web.HTTPNotFound()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT p.file_id,m.data FROM ad_photos p LEFT JOIN miniapp_media m ON m.file_id=p.file_id "
                "WHERE p.ad_id=? ORDER BY p.sort_order,p.file_id LIMIT 1 OFFSET ?", (ad_id, index)
            ).fetchone()
        if not row:
            raise web.HTTPNotFound()
        if row["data"] is not None:
            content = row["data"]
        else:
            remote = await application.bot.get_file(row["file_id"])
            if remote.file_size and remote.file_size > 10_000_000:
                raise web.HTTPRequestEntityTooLarge(max_size=10_000_000, actual_size=remote.file_size)
            content = bytes(await remote.download_as_bytearray())
            if len(content) > 10_000_000:
                raise web.HTTPRequestEntityTooLarge(max_size=10_000_000, actual_size=len(content))
        return web.Response(body=content, content_type="image/jpeg")

    async def sync_archived(ad):
        messages = market.publication_messages(ad["ad_id"])
        with db.connect() as conn:
            count = conn.execute("SELECT count(*) FROM ad_photos WHERE ad_id=?", (ad["ad_id"],)).fetchone()[0]
        return await sync_group(application, ad, "archived", messages, count)

    def audit_sync_failure(admin, ad_id):
        with db.transaction() as conn:
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'ad_group_sync_failed',?)",
                         (admin, str(ad_id)))

    async def ad_action(request):
        ad_id = int(request.match_info["ad_id"])
        body = await request.json()
        if not isinstance(body, dict) or body.get("action") not in {"approve", "reject", "archive", "restore", "sync"}:
            raise MarketplaceError("عملیات آگهی معتبر نیست.")
        note = body.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise MarketplaceError("یادداشت معتبر نیست.")
        action, admin = body["action"], request["user"]["id"]
        async with lock:
            ad = market.get_ad(ad_id)
            if not ad:
                raise web.HTTPNotFound()
            if action == "sync":
                if ad["status"] != "archived" or not ad["publication_message_id"]:
                    raise MarketplaceError("پیام نشرشده‌ای برای همگام‌سازی وجود ندارد.")
                if not await sync_archived(ad):
                    audit_sync_failure(admin, ad_id)
                    return web.json_response({"error": "پیام گروه به‌روز نشد؛ اجازهٔ ربات و پیام گروه را بررسی کنید."}, status=502)
                with db.transaction() as conn:
                    conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'ad_group_sync',?)", (admin, str(ad_id)))
                return web.json_response({"status": "archived", "group_synced": True})
            if action == "restore":
                if ad["status"] != "archived":
                    raise MarketplaceError("فقط آگهی بایگانی‌شده قابل بازیابی است.")
                if ad["publication_message_id"] and not await sync_archived(ad):
                    audit_sync_failure(admin, ad_id)
                    return web.json_response({"error": "پیام قبلی گروه هنوز همگام نیست؛ پیش از بازیابی آن را بررسی کنید."}, status=502)
                with db.transaction() as conn:
                    row = conn.execute("SELECT * FROM ads WHERE ad_id=? AND status='archived'", (ad_id,)).fetchone()
                    if not row:
                        raise MarketplaceError("وضعیت آگهی تغییر کرده است.")
                    if conn.execute("SELECT 1 FROM ads WHERE owner_id=? AND status='draft' AND ad_id!=?", (row["owner_id"], ad_id)).fetchone():
                        raise MarketplaceError("کاربر پیش‌نویس دیگری دارد؛ ابتدا آن را تکمیل کند.")
                    conn.execute(
                        "INSERT OR IGNORE INTO ad_publication_history(ad_id,chat_id,message_id) "
                        "SELECT ad_id,chat_id,message_id FROM ad_publication_messages WHERE ad_id=?", (ad_id,)
                    )
                    if row["publication_chat_id"] and row["publication_message_id"]:
                        conn.execute("INSERT OR IGNORE INTO ad_publication_history(ad_id,chat_id,message_id) VALUES(?,?,?)",
                                     (ad_id, row["publication_chat_id"], row["publication_message_id"]))
                    conn.execute("DELETE FROM ad_publication_messages WHERE ad_id=?", (ad_id,))
                    conn.execute(
                        "UPDATE ads SET status='draft',publication_chat_id=NULL,publication_message_id=NULL,"
                        "published_at=NULL,publish_claimed_at=NULL,review_note='',miniapp_revision=miniapp_revision+1,"
                        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=?", (ad_id,)
                    )
                    conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (admin,))
                    conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'ad_restore',?)", (admin, str(ad_id)))
                    conn.execute("INSERT INTO moderation_events(user_id,admin_id,ad_id,action,reason) VALUES(?,?,?,'restore','')",
                                 (row["owner_id"], admin, ad_id))
                return web.json_response({"status": "draft", "group_synced": True})
            target = {"approve": "approved", "reject": "rejected", "archive": "archived"}[action]
            market.transition(ad_id, target, admin, note)
            if action == "archive" and ad["status"] == "published":
                synced = await sync_archived(ad)
                if not synced:
                    audit_sync_failure(admin, ad_id)
                    return web.json_response({"error": "آگهی بایگانی شد، اما پیام گروه به‌روز نشد؛ همگام‌سازی را دوباره انجام دهید.", "status": "archived"}, status=502)
            if action == "approve":
                from handlers.admin import _publish_ad
                await _publish_ad(SimpleNamespace(application=application, bot=application.bot), ad_id, admin)
            elif action == "reject":
                try:
                    await application.bot.send_message(ad["owner_id"], f"آگهی {ad_id} رد شد. یادداشت مدیر: {note or 'با مدیران تماس بگیرید.'}")
                except TelegramError as exc:
                    logger.info("Unable to notify owner of ad review %s: %s", ad_id, type(exc).__name__)
                    return web.json_response({"status": target, "owner_notified": False})
            return web.json_response({"status": market.get_ad(ad_id)["status"], "group_synced": True})

    async def featured(request):
        ad_id = int(request.match_info["ad_id"])
        body = await request.json()
        if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
            raise MarketplaceError("وضعیت ویژه معتبر نیست.")
        days = body.get("days", 7)
        if type(days) is not int or not 1 <= days <= 365:
            raise MarketplaceError("مدت ویژه باید از ۱ تا ۳۶۵ روز باشد.")
        until = (datetime.now(UTC) + timedelta(days=days)).isoformat() if body["enabled"] else None
        with db.transaction() as conn:
            if conn.execute("UPDATE ads SET featured_until=? WHERE ad_id=? AND status='published'", (until, ad_id)).rowcount != 1:
                raise MarketplaceError("فقط آگهی نشرشده را می‌توان ویژه کرد.")
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'featured_set',?)",
                         (request["user"]["id"], f"{ad_id}:{until or 'off'}"))
        return web.json_response({"featured_until": until})

    async def reports(request):
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT r.report_id,r.ad_id,r.reporter_id,r.category,r.description,r.status,r.created_at,a.title "
                "FROM reports r JOIN ads a USING(ad_id) ORDER BY r.report_id DESC LIMIT 51 OFFSET ?",
                (page(request) * 50,),
            ).fetchall()
        return web.json_response({"items": [dict(row) for row in rows[:50]], "has_more": len(rows) > 50})

    async def report_action(request):
        body = await request.json()
        if not isinstance(body, dict) or body.get("action") not in {"review", "dismiss"}:
            raise MarketplaceError("عملیات گزارش معتبر نیست.")
        market.review_report(int(request.match_info["report_id"]), request["user"]["id"], body["action"] == "dismiss")
        return web.json_response({"ok": True})

    async def history(request):
        with db.connect() as conn:
            rows = conn.execute("SELECT action_id,admin_id,action,target,created_at FROM admin_actions ORDER BY action_id DESC LIMIT 51 OFFSET ?",
                                (page(request) * 50,)).fetchall()
        return web.json_response({"items": [dict(row) for row in rows[:50]], "has_more": len(rows) > 50})

    app.router.add_get("/api/admin/ads", ads)
    app.router.add_get("/api/admin/ads/{ad_id}/photo/{index}", ad_photo)
    app.router.add_post("/api/admin/ads/{ad_id}/action", ad_action)
    app.router.add_post("/api/admin/ads/{ad_id}/featured", featured)
    app.router.add_get("/api/admin/reports", reports)
    app.router.add_post("/api/admin/reports/{report_id}", report_action)
    app.router.add_get("/api/admin/history", history)
