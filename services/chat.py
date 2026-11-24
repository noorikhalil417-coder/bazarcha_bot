"""Private ad conversations. Content is available only to participants."""

import asyncio
import hashlib
import logging
import re
import sqlite3

from aiohttp import web
from telegram.error import TelegramError

from services.marketplace import MarketplaceError, format_price

logger = logging.getLogger(__name__)


def register_chat_routes(app, application, normalize_photo, download_photo):
    market = application.bot_data["marketplace"]
    db = market.db
    image_slots = asyncio.Semaphore(4)

    def participant(conn, conversation_id, user_id):
        row = conn.execute(
            "SELECT c.*,a.title,a.price_minor,a.price_negotiable,a.currency,a.status ad_status,"
            "(SELECT count(*) FROM ad_photos p WHERE p.ad_id=a.ad_id) photo_count "
            "FROM conversations c JOIN ads a USING(ad_id) "
            "WHERE c.conversation_id=? AND (c.buyer_id=? OR c.seller_id=?)",
            (conversation_id, user_id, user_id),
        ).fetchone()
        if not row:
            raise web.HTTPNotFound()
        return row

    def other(row, uid):
        return row["seller_id"] if row["buyer_id"] == uid else row["buyer_id"]

    def block_state(conn, uid, peer):
        return conn.execute(
            "SELECT blocker_id FROM chat_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",
            (uid, peer, peer, uid),
        ).fetchall()

    def unread_count(conn, uid):
        return conn.execute(
            "SELECT count(*) FROM chat_messages m JOIN conversations c USING(conversation_id) "
            "WHERE (c.buyer_id=? OR c.seller_id=?) AND m.sender_id!=? AND m.message_id>"
            "coalesce((SELECT last_message_id FROM chat_reads r WHERE r.conversation_id=c.conversation_id AND r.user_id=?),0)",
            (uid, uid, uid, uid),
        ).fetchone()[0]

    def describe(conn, row, uid):
        peer = other(row, uid)
        name = conn.execute("SELECT display_name,username FROM users WHERE user_id=?", (peer,)).fetchone()
        blocked = block_state(conn, uid, peer)
        unread = conn.execute(
            "SELECT count(*) FROM chat_messages WHERE conversation_id=? AND sender_id!=? AND message_id>"
            "coalesce((SELECT last_message_id FROM chat_reads WHERE conversation_id=? AND user_id=?),0)",
            (row["conversation_id"], uid, row["conversation_id"], uid),
        ).fetchone()[0]
        return {
            "conversation_id": row["conversation_id"], "ad_id": row["ad_id"], "title": row["title"],
            "price": format_price(row), "photo_count": row["photo_count"], "ad_status": row["ad_status"],
            "peer_id": peer, "peer_name": (name["display_name"] or name["username"] or "کاربر") if name else "کاربر",
            "blocked_by_me": any(r["blocker_id"] == uid for r in blocked),
            "blocked": bool(blocked), "unread": unread,
        }

    async def start(request):
        body = await request.json()
        ad_id, uid = int(body["ad_id"]), request["user"]["id"]
        market.ensure_user(uid, request["user"].get("username"), request["user"].get("first_name", ""))
        with db.transaction() as conn:
            ad = conn.execute("SELECT owner_id,status FROM ads WHERE ad_id=?", (ad_id,)).fetchone()
            if not ad or ad["status"] != "published":
                raise web.HTTPNotFound()
            seller = ad["owner_id"]
            if seller == uid:
                raise MarketplaceError("نمی‌توانید برای آگهی خود گفت‌وگو بسازید.")
            if block_state(conn, uid, seller) or conn.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (seller,)).fetchone():
                raise MarketplaceError("امکان شروع گفت‌وگو با این کاربر وجود ندارد.")
            conn.execute("INSERT OR IGNORE INTO conversations(ad_id,buyer_id,seller_id) VALUES(?,?,?)", (ad_id, uid, seller))
            cid = conn.execute("SELECT conversation_id FROM conversations WHERE ad_id=? AND buyer_id=? AND seller_id=?",
                               (ad_id, uid, seller)).fetchone()[0]
            result = describe(conn, participant(conn, cid, uid), uid)
        return web.json_response(result)

    async def conversations(request):
        uid = request["user"]["id"]
        search = request.query.get("q", "").strip()
        if len(search) > 80:
            raise MarketplaceError("متن جست‌وجو طولانی است.")
        page = max(0, min(int(request.query.get("page", 0)), 10000))
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT c.conversation_id FROM conversations c JOIN ads a USING(ad_id) "
                "JOIN users u ON u.user_id=CASE WHEN c.buyer_id=? THEN c.seller_id ELSE c.buyer_id END "
                "WHERE (c.buyer_id=? OR c.seller_id=?) AND (instr(a.title,?)>0 OR instr(u.display_name,?)>0 OR instr(coalesce(u.username,''),?)>0) "
                "ORDER BY coalesce((SELECT max(message_id) FROM chat_messages WHERE conversation_id=c.conversation_id),0) DESC,c.conversation_id DESC LIMIT 51 OFFSET ?",
                (uid, uid, uid, search, search, search, page * 50),
            ).fetchall()
            items = [describe(conn, participant(conn, row[0], uid), uid) for row in rows[:50]]
            unread = unread_count(conn, uid)
        return web.json_response({"items": items, "unread": unread, "has_more": len(rows) > 50})

    async def unread(request):
        with db.connect() as conn:
            count = unread_count(conn, request["user"]["id"])
        return web.json_response({"unread": count})

    async def messages(request):
        uid, cid = request["user"]["id"], int(request.match_info["cid"])
        before = int(request.query.get("before", "0"))
        if before < 0:
            raise MarketplaceError("صفحه معتبر نیست.")
        with db.connect() as conn:
            row = participant(conn, cid, uid)
            info = describe(conn, row, uid)
            read = conn.execute("SELECT last_message_id FROM chat_reads WHERE conversation_id=? AND user_id=?",
                                (cid, other(row, uid))).fetchone()
            peer_read = read[0] if read else 0
            rows = conn.execute(
                "SELECT message_id,sender_id,body,image IS NOT NULL has_image,created_at FROM chat_messages "
                "WHERE conversation_id=? AND (?=0 OR message_id<?) ORDER BY message_id DESC LIMIT 51",
                (cid, before, before),
            ).fetchall()
            items = [{**dict(m), "mine": m["sender_id"] == uid,
                      "read": m["sender_id"] == uid and m["message_id"] <= peer_read} for m in reversed(rows[:50])]
        return web.json_response({"conversation": info, "items": items, "has_more": len(rows) > 50})

    async def send(request):
        uid, cid = request["user"]["id"], int(request.match_info["cid"])
        body = await request.json()
        text, key, encoded = body.get("text", ""), body.get("client_id", ""), body.get("image", "")
        if not isinstance(text, str) or len(text.strip()) > 3000 or not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", key):
            raise MarketplaceError("متن یا شناسهٔ پیام معتبر نیست.")
        if not isinstance(encoded, str):
            raise MarketplaceError("تصویر معتبر نیست.")
        text = text.strip()
        if not text and not encoded:
            raise MarketplaceError("متن یا تصویر پیام لازم است.")
        with db.connect() as conn:
            participant(conn, cid, uid)
        if encoded:
            async with image_slots:
                image = await asyncio.to_thread(normalize_photo, encoded)
        else:
            image = None
        fingerprint = hashlib.sha256(text.encode() + b"\0" + (image or b"")).hexdigest()
        notify = False
        with db.transaction() as conn:
            row = participant(conn, cid, uid)
            peer = other(row, uid)
            previous = conn.execute(
                "SELECT message_id,fingerprint FROM chat_messages WHERE conversation_id=? AND sender_id=? AND client_id=?",
                (cid, uid, key),
            ).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise MarketplaceError("شناسهٔ پیام برای محتوای دیگری استفاده شده است.")
                return web.json_response({"message_id": previous["message_id"], "duplicate": True})
            if block_state(conn, uid, peer) or conn.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (peer,)).fetchone():
                raise MarketplaceError("ارسال پیام در این گفت‌وگو مسدود است.")
            if conn.execute("SELECT count(*) FROM chat_messages WHERE sender_id=? AND created_at>=strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 minute')", (uid,)).fetchone()[0] >= 20:
                raise MarketplaceError("پیام‌های زیادی فرستاده‌اید؛ کمی صبر کنید.")
            if conn.execute(
                "SELECT 1 FROM chat_messages WHERE sender_id=? AND conversation_id=? AND fingerprint=? "
                "AND created_at>=strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 seconds')",
                (uid, cid, fingerprint),
            ).fetchone():
                raise MarketplaceError("این پیام به‌تازگی فرستاده شده است.")
            message_id = conn.execute(
                "INSERT INTO chat_messages(conversation_id,sender_id,client_id,body,image,fingerprint) VALUES(?,?,?,?,?,?)",
                (cid, uid, key, text, image, fingerprint),
            ).lastrowid
            notify = conn.execute(
                "INSERT INTO chat_notifications(user_id,last_sent_at) VALUES(?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(user_id) DO UPDATE SET last_sent_at=excluded.last_sent_at "
                "WHERE last_sent_at<strftime('%Y-%m-%dT%H:%M:%fZ','now','-5 minutes')",
                (peer,),
            ).rowcount == 1
        notified = None
        if notify:
            try:
                await application.bot.send_message(peer, "پیام تازه‌ای در دوکانچه دارید. برای دیدن آن مینی‌اپ را باز کنید.")
                notified = True
            except TelegramError as exc:
                logger.info("Chat notification failed for user %s: %s", peer, type(exc).__name__)
                notified = False
        return web.json_response({"message_id": message_id, "duplicate": False, "notified": notified})

    async def read(request):
        uid, cid = request["user"]["id"], int(request.match_info["cid"])
        body = await request.json()
        value = body.get("through")
        if type(value) is not int or value <= 0:
            raise MarketplaceError("شناسهٔ پیام معتبر نیست.")
        with db.transaction() as conn:
            participant(conn, cid, uid)
            if not conn.execute("SELECT 1 FROM chat_messages WHERE conversation_id=? AND message_id=?", (cid, value)).fetchone():
                raise MarketplaceError("پیام در این گفت‌وگو پیدا نشد.")
            conn.execute(
                "INSERT INTO chat_reads(conversation_id,user_id,last_message_id) VALUES(?,?,?) "
                "ON CONFLICT(conversation_id,user_id) DO UPDATE SET last_message_id=max(last_message_id,excluded.last_message_id)",
                (cid, uid, value),
            )
        return web.json_response({"ok": True})

    async def image(request):
        uid, cid, mid = request["user"]["id"], int(request.match_info["cid"]), int(request.match_info["mid"])
        with db.connect() as conn:
            participant(conn, cid, uid)
            row = conn.execute("SELECT image FROM chat_messages WHERE conversation_id=? AND message_id=?", (cid, mid)).fetchone()
        if not row or row["image"] is None:
            raise web.HTTPNotFound()
        return web.Response(body=row["image"], content_type="image/jpeg")

    async def ad_photo(request):
        uid, cid = request["user"]["id"], int(request.match_info["cid"])
        with db.connect() as conn:
            row = participant(conn, cid, uid)
            photo = conn.execute(
                "SELECT p.file_id,m.data FROM ad_photos p LEFT JOIN miniapp_media m ON m.file_id=p.file_id "
                "WHERE p.ad_id=? ORDER BY p.sort_order,p.file_id LIMIT 1", (row["ad_id"],),
            ).fetchone()
        if not photo:
            raise web.HTTPNotFound()
        content = photo["data"] if photo["data"] is not None else await download_photo(photo["file_id"])
        return web.Response(body=content, content_type="image/jpeg")

    async def block(request):
        uid, cid = request["user"]["id"], int(request.match_info["cid"])
        body = await request.json()
        if type(body.get("blocked")) is not bool:
            raise MarketplaceError("وضعیت مسدودسازی معتبر نیست.")
        with db.transaction() as conn:
            peer = other(participant(conn, cid, uid), uid)
            if body["blocked"]:
                conn.execute("INSERT OR IGNORE INTO chat_blocks(blocker_id,blocked_id) VALUES(?,?)", (uid, peer))
            else:
                conn.execute("DELETE FROM chat_blocks WHERE blocker_id=? AND blocked_id=?", (uid, peer))
        return web.json_response({"ok": True})

    async def report(request):
        uid, cid = request["user"]["id"], int(request.match_info["cid"])
        body = await request.json()
        mid, reason = body.get("message_id"), body.get("reason")
        if type(mid) is not int or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise MarketplaceError("پیام و دلیل گزارش معتبر نیست.")
        with db.transaction() as conn:
            participant(conn, cid, uid)
            if not conn.execute("SELECT 1 FROM chat_messages WHERE message_id=? AND conversation_id=? AND sender_id!=?",
                                (mid, cid, uid)).fetchone():
                raise MarketplaceError("فقط پیام طرف مقابل قابل گزارش است.")
            try:
                rid = conn.execute("INSERT INTO chat_reports(conversation_id,message_id,reporter_id,reason) VALUES(?,?,?,?)",
                                   (cid, mid, uid, reason.strip())).lastrowid
            except sqlite3.IntegrityError as exc:
                raise MarketplaceError("این پیام قبلاً گزارش شده است.") from exc
        return web.json_response({"report_id": rid})

    def reviewer(request):
        if request["user"]["id"] not in application.bot_data["config"].chat_reviewer_ids:
            raise web.HTTPForbidden()
        return request["user"]["id"]

    async def reports(request):
        reviewer(request)
        before = int(request.query.get("before", "0"))
        if before < 0:
            raise MarketplaceError("صفحه معتبر نیست.")
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT report_id,conversation_id,message_id,reporter_id,reason,status,created_at "
                "FROM chat_reports WHERE (?=0 OR report_id<?) ORDER BY report_id DESC LIMIT 51",
                (before, before),
            ).fetchall()
        return web.json_response({"items": [dict(row) for row in rows[:50]], "has_more": len(rows) > 50})

    async def evidence(request):
        admin, report_id = reviewer(request), int(request.match_info["report_id"])
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT r.report_id,r.status,m.message_id,m.sender_id,m.body,m.image IS NOT NULL has_image,m.created_at "
                "FROM chat_reports r JOIN chat_messages m ON m.message_id=r.message_id "
                "AND m.conversation_id=r.conversation_id WHERE r.report_id=?", (report_id,)
            ).fetchone()
            if not row:
                raise web.HTTPNotFound()
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'chat_report_view',?)", (admin, str(report_id)))
        return web.json_response(dict(row))

    async def evidence_image(request):
        admin, report_id = reviewer(request), int(request.match_info["report_id"])
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT m.image FROM chat_reports r JOIN chat_messages m ON m.message_id=r.message_id "
                "AND m.conversation_id=r.conversation_id WHERE r.report_id=?", (report_id,)
            ).fetchone()
            if not row or row["image"] is None:
                raise web.HTTPNotFound()
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'chat_report_image_view',?)", (admin, str(report_id)))
        return web.Response(body=row["image"], content_type="image/jpeg")

    async def review(request):
        admin, report_id = reviewer(request), int(request.match_info["report_id"])
        with db.transaction() as conn:
            if conn.execute("UPDATE chat_reports SET status='reviewed' WHERE report_id=? AND status='open'", (report_id,)).rowcount != 1:
                raise MarketplaceError("گزارش پیدا نشد یا قبلاً بررسی شده است.")
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'chat_report_review',?)", (admin, str(report_id)))
        return web.json_response({"ok": True})

    app.router.add_post("/api/chat/start", start)
    app.router.add_get("/api/chat/conversations", conversations)
    app.router.add_get("/api/chat/unread", unread)
    app.router.add_get("/api/chat/{cid}/messages", messages)
    app.router.add_post("/api/chat/{cid}/messages", send)
    app.router.add_post("/api/chat/{cid}/read", read)
    app.router.add_get("/api/chat/{cid}/image/{mid}", image)
    app.router.add_get("/api/chat/{cid}/ad-photo", ad_photo)
    app.router.add_post("/api/chat/{cid}/block", block)
    app.router.add_post("/api/chat/{cid}/report", report)
    app.router.add_get("/api/admin/chat-reports", reports)
    app.router.add_get("/api/admin/chat-reports/{report_id}", evidence)
    app.router.add_get("/api/admin/chat-reports/{report_id}/image", evidence_image)
    app.router.add_post("/api/admin/chat-reports/{report_id}/review", review)
