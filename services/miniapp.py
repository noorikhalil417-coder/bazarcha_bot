"""Same-origin Telegram Mini App: signed identity, durable drafts, existing review queue."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl

from aiohttp import web
from PIL import Image, ImageOps, UnidentifiedImageError

from services.marketplace import MarketplaceError, parse_price_input, validate_ad
from services.action_locks import action_lock
from services.telegram_media import download_telegram_photo

logger = logging.getLogger(__name__)
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
Image.MAX_IMAGE_PIXELS = 16_000_000
PREAUTH_REQUESTS_PER_MINUTE = 3000


def validate_init_data(raw: str, token: str, now: int | None = None) -> dict:
    """Telegram HMAC validation; never trust initDataUnsafe or client owner IDs."""
    if not raw or len(raw) > 8192:
        raise ValueError("ورود معتبر تلگرام لازم است؛ صفحه را از ربات باز کنید.")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("اطلاعات ورود معتبر نیست.")
    supplied = fields.pop("hash", "")
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise ValueError("ورود معتبر نیست؛ صفحه را از ربات دوباره باز کنید.")
    current = int(time.time()) if now is None else now
    issued = int(fields.get("auth_date", "0"))
    if not current - 3600 <= issued <= current + 30:
        raise ValueError("نشست پایان یافته؛ صفحه را از ربات دوباره باز کنید.")
    user = json.loads(fields.get("user", "null"))
    if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
        raise ValueError("اطلاعات کاربر معتبر نیست.")
    return user


def extract_fields(text: str, categories: list) -> dict:
    """Conservative text assistance; only explicit amounts/cities, editable category suggestion."""
    text = text.strip()[:3000]
    result = {"description": text, "title": re.split(r"[،,\n]", text)[0].strip()[:120]}
    amounts = re.findall(r"(?<![\w])([۰-۹٠-٩0-9]+(?:[٬,][۰-۹٠-٩0-9]{3})*(?:\s*هزار)?\s*(?:افغانی|AFN))", text, re.I)
    if len(amounts) == 1:
        try:
            parse_price_input(amounts[0])
            result["price"] = amounts[0]
        except MarketplaceError:
            pass
    elif re.search(r"(?:توافقی|با\s*جورآمد)", text) and not re.search(r"[۰-۹٠-٩0-9]", text):
        result["price"] = "توافقی"
    cities = [city for city in ("مزارشریف", "مزار شریف", "کابل", "هرات", "قندهار", "جلال‌آباد", "کندز", "بامیان", "غزنی") if city in text]
    if len(cities) == 1:
        result["city"] = cities[0]
    rules = [(r"موبایل|سامسونگ|آیفون|لپتاپ", "موبایل"), (r"موتر|موترسایکل", "موتر"), (r"خانه|آپارتمان|زمین", "جایداد"), (r"لباس|پیراهن|کفش", "لباس"), (r"برقکار|برق‌کار|ترمیم|نجار", "خدمات")]
    matches = [label for pattern, label in rules if re.search(pattern, text)]
    if len(matches) == 1:
        for category in categories:
            if matches[0] in category["name"]:
                result["category_id"] = category["category_id"]
                break
    return result


def normalize_photo(encoded: str) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > 4_000_000:
        raise MarketplaceError("عکس بزرگ است؛ عکس کوچک‌تری انتخاب کنید.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError
            if image.width * image.height > 16_000_000:
                raise ValueError
            image = ImageOps.exif_transpose(image)
            image.load()
            image.thumbnail((1600, 1600))
            output = io.BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=85)
            content = output.getvalue()
            if len(content) > 2_097_152:
                raise ValueError
            return content
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise MarketplaceError("عکس معتبر JPG، PNG یا WEBP بفرستید.") from exc


def create_web_app(application) -> web.Application:
    market = application.bot_data["marketplace"]
    config = application.bot_data["config"]
    lock = action_lock(application, "market")
    rates: OrderedDict = OrderedDict()
    peer_rates: OrderedDict = OrderedDict()

    @web.middleware
    async def security(request, handler):
        try:
            if request.path.startswith("/api/"):
                now = time.monotonic()
                peer = request.remote or "unknown"
                count, started = peer_rates.pop(peer, (0, now))
                if now - started >= 60:
                    count, started = 0, now
                peer_rates[peer] = (count + 1, started)
                if len(peer_rates) > 5000:
                    peer_rates.popitem(last=False)
                if count >= PREAUTH_REQUESTS_PER_MINUTE:
                    return web.json_response({"error": "درخواست‌های زیادی ارسال شده است؛ کمی صبر کنید."}, status=429)
                auth = request.headers.get("Authorization", "")
                try:
                    user = validate_init_data(auth.removeprefix("tma ") if auth.startswith("tma ") else "", config.token)
                except (ValueError, TypeError, json.JSONDecodeError):
                    return web.json_response({"error": "صفحه را از دکمهٔ داخل ربات دوباره باز کنید."}, status=401)
                request["user"] = user
                # All API calls are authenticated, bounded, same-origin JSON. No permissive CORS.
                if request.method != "GET":
                    if request.content_type != "application/json":
                        return web.json_response({"error": "درخواست معتبر نیست."}, status=415)
                    if request.headers.get("Origin") != config.mini_app_url:
                        return web.json_response({"error": "مبدأ درخواست معتبر نیست."}, status=403)
                now = time.monotonic()
                uid = user["id"]
                count, started = rates.pop(uid, (0, now))
                if now - started >= 60:
                    count, started = 0, now
                rates[uid] = (count + 1, started)
                if len(rates) > 5000:
                    rates.popitem(last=False)
                if count >= 90:
                    return web.json_response({"error": "کمی صبر کنید و دوباره تلاش کنید."}, status=429)
                if user["id"] not in config.admin_ids:
                    with market.db.connect() as conn:
                        blocked = conn.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (user["id"],)).fetchone()
                    if blocked:
                        return web.json_response({"error": "دسترسی این حساب مسدود شده است."}, status=403)
                if request.path.startswith("/api/admin/") and user["id"] not in config.admin_ids:
                    return web.json_response({"error": "دسترسی مدیر لازم است."}, status=403)
            response = await handler(request)
        except MarketplaceError as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            response = web.json_response({"error": "اطلاعات معتبر نیست؛ بخش‌ها را بررسی کنید."}, status=400)
        except web.HTTPException:
            raise
        except Exception as exc:
            logger.error("Mini App request failed: %s", type(exc).__name__)
            response = web.json_response({"error": "درخواست انجام نشد؛ دوباره تلاش کنید."}, status=500)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "Permissions-Policy": "camera=(), microphone=(), geolocation=()", "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'"})
        return response

    app = web.Application(middlewares=[security], client_max_size=4_500_000)

    def draft_payload(ad):
        if not ad:
            return None
        data = {key: ad[key] for key in ("ad_id", "title", "description", "category_id", "ad_type", "city", "contact", "miniapp_revision", "status")}
        data["price"] = str(ad["price_minor"]) if ad["price_minor"] is not None else ("توافقی" if ad["price_negotiable"] else "")
        with market.db.connect() as conn:
            data["photos"] = [row[0] for row in conn.execute("SELECT file_id FROM miniapp_media WHERE ad_id=? ORDER BY rowid", (ad["ad_id"],))]
            data["legacy_photo_count"] = conn.execute("SELECT count(*) FROM ad_photos WHERE ad_id=? AND file_id NOT LIKE 'miniapp:%'", (ad["ad_id"],)).fetchone()[0]
        return data

    async def state(request):
        user = request["user"]
        with market.db.connect() as conn:
            provinces = [row[0] for row in conn.execute("SELECT name FROM provinces WHERE enabled=1 ORDER BY sort_order,province_id")]
        return web.json_response({"user": {"name": user.get("first_name", "دوست عزیز"), "username": user.get("username", "")}, "categories": [dict(row) for row in market.categories()], "provinces": provinces, "draft": draft_payload(market.draft_for_user(user["id"])), "ads": application.bot_data["miniapp_cards"](user["id"]), "profile": application.bot_data["miniapp_profile"](user["id"]), "rules": market.get_setting("rules", config.group_rules)})

    async def extract(request):
        body = await request.json()
        if not isinstance(body.get("text"), str):
            raise ValueError
        return web.json_response(extract_fields(body["text"], market.categories()))

    async def save(request):
        body = await request.json()
        user = request["user"]
        allowed_categories = {row["category_id"] for row in market.categories()}
        category = int(body["category_id"])
        if category not in allowed_categories:
            raise MarketplaceError("دسته‌بندی فعال را انتخاب کنید.")
        price, negotiable = parse_price_input(str(body.get("price", "")))
        if body.get("contact_mode") == "telegram":
            username = user.get("username", "")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                raise MarketplaceError("حساب شما نام کاربری ندارد؛ شماره تماس را انتخاب کنید.")
            contact = "https://t.me/" + username
        elif body.get("contact_mode") == "phone":
            contact = str(body.get("contact", "")).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
            contact = re.sub(r"[ ()-]", "", contact)
            if not re.fullmatch(r"\+?[0-9]{8,15}", contact):
                raise MarketplaceError("شماره تماس معتبر با ۸ تا ۱۵ رقم وارد کنید.")
        else:
            raise MarketplaceError("راه تماس را انتخاب کنید.")
        if body.get("contact_consent") is not True:
            raise MarketplaceError("نشر راه تماس را تأیید کنید.")
        data = validate_ad({"title": body.get("title", ""), "description": body.get("description", ""), "city": body.get("city", ""), "contact": contact, "ad_type": body.get("ad_type"), "price_minor": price, "price_negotiable": negotiable})
        async with lock:
            market.ensure_user(user["id"], user.get("username"), user.get("first_name", ""))
            ad_id = int(body.get("ad_id") or 0)
            if not ad_id:
                existing = market.draft_for_user(user["id"])
                if existing:
                    raise MarketplaceError("پیش‌نویس موجود است؛ صفحه را تازه کنید تا همان را ادامه دهید.")
                ad_id = market.create_draft(user["id"], category, data["ad_type"])
            with market.db.transaction() as conn:
                ad = conn.execute("SELECT * FROM ads WHERE ad_id=? AND owner_id=? AND status='draft'", (ad_id, user["id"])).fetchone()
                if not ad:
                    raise MarketplaceError("این پیش‌نویس در دسترس نیست.")
                if ad["miniapp_revision"] != int(body.get("miniapp_revision", 0)):
                    raise MarketplaceError("پیش‌نویس در صفحهٔ دیگری تغییر کرده؛ صفحه را تازه کنید.")
                conn.execute("UPDATE ads SET title=?,description=?,city=?,contact=?,ad_type=?,price_minor=?,price_negotiable=?,category_id=?,miniapp_revision=miniapp_revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=?", (data["title"], data["description"], data["city"], contact, data["ad_type"], price, int(negotiable), category, ad_id))
        return web.json_response(draft_payload(market.owned_ad(ad_id, user["id"])))

    async def photo(request):
        body = await request.json()
        content = await asyncio.to_thread(normalize_photo, body.get("image"))
        async with lock:
            ad_id = int(body["ad_id"])
            owner_id = request["user"]["id"]
            with market.db.transaction() as conn:
                if not conn.execute("SELECT 1 FROM ads WHERE ad_id=? AND owner_id=? AND status='draft'", (ad_id, owner_id)).fetchone():
                    raise MarketplaceError("پیش‌نویس در دسترس نیست.")
                count = conn.execute("SELECT count(*) FROM ad_photos WHERE ad_id=?", (ad_id,)).fetchone()[0]
                if count >= 10:
                    raise MarketplaceError("حداکثر ۱۰ عکس پذیرفته می‌شود.")
                file_id = "miniapp:" + uuid.uuid4().hex
                conn.execute("INSERT INTO miniapp_media VALUES(?,?,?)", (file_id, ad_id, content))
                conn.execute("INSERT INTO ad_photos VALUES(?,?,?)", (ad_id, file_id, count))
                conn.execute("UPDATE ads SET miniapp_revision=miniapp_revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=?", (ad_id,))
        return web.json_response(draft_payload(market.owned_ad(ad_id, owner_id)))

    async def media(request):
        with market.db.connect() as conn:
            row = conn.execute("SELECT m.data FROM miniapp_media m JOIN ads a ON a.ad_id=m.ad_id WHERE m.file_id=? AND a.owner_id=?", (request.match_info["file_id"], request["user"]["id"])).fetchone()
        if not row:
            raise web.HTTPNotFound()
        return web.Response(body=row[0], content_type="image/jpeg")

    async def delete_photo(request):
        body = await request.json()
        async with lock:
            with market.db.transaction() as conn:
                row = conn.execute("SELECT m.ad_id FROM miniapp_media m JOIN ads a ON a.ad_id=m.ad_id WHERE m.file_id=? AND a.owner_id=? AND a.status='draft'", (body["file_id"], request["user"]["id"])).fetchone()
                if not row:
                    raise MarketplaceError("عکس در دسترس نیست.")
                conn.execute("DELETE FROM ad_photos WHERE file_id=?", (body["file_id"],))
                conn.execute("DELETE FROM miniapp_media WHERE file_id=?", (body["file_id"],))
                conn.execute("UPDATE ads SET miniapp_revision=miniapp_revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=?", (row[0],))
        return web.json_response(draft_payload(market.owned_ad(row[0], request["user"]["id"])))

    async def submit(request):
        body = await request.json()
        ad_id = int(body["ad_id"])
        owner_id = request["user"]["id"]
        async with lock:
            ad = market.owned_ad(ad_id, owner_id)
            if not ad:
                raise MarketplaceError("آگهی در دسترس نیست.")
            if ad["status"] != "draft":
                return web.json_response({"ad_id": ad_id, "status": ad["status"]})
            if ad["miniapp_revision"] != int(body["miniapp_revision"]):
                raise MarketplaceError("پیش‌نویس تغییر کرده؛ پیش‌نمایش را دوباره بررسی کنید.")
            # Enforce limit again at submission; drafts do not reserve active slots.
            with market.db.connect() as conn:
                count = conn.execute("SELECT count(*) FROM ads WHERE owner_id=? AND status IN ('pending','approved','publishing','published')", (owner_id,)).fetchone()[0]
            if count >= market.max_active_ads:
                raise MarketplaceError("به سقف آگهی‌های فعال رسیده‌اید.")
            market.submit(ad_id, owner_id)
        from handlers.admin import notify_ad_reviewers
        context = SimpleNamespace(application=application, bot=application.bot)
        await notify_ad_reviewers(context, ad_id)
        try:
            await application.bot.send_message(owner_id, f"✅ آگهی <b>#{ad_id}</b> برای بررسی مدیر فرستاده شد.\nپس از تأیید در دوکانچه نشر می‌شود.", parse_mode="HTML")
        except Exception as exc:
            logger.info("Mini App confirmation unavailable: %s", type(exc).__name__)
        return web.json_response({"ad_id": ad_id, "status": "pending"})

    async def static(request):
        name = request.match_info.get("name", "index.html")
        if name not in {"index.html", "app.js", "banners.js", "chat.js", "chat.css", "style.css", "vazirmatn.woff2", "FONT-LICENSE.txt", "logo.svg", "approved-logo.png", "approved-logo-small.png", "admin.html", "admin.js", "admin.css", "admin-chat.js"}:
            raise web.HTTPNotFound()
        return web.FileResponse(WEB_ROOT / name)

    async def admin_summary(request):
        with market.db.connect() as conn:
            users = conn.execute("SELECT count(*) FROM users").fetchone()[0]
            ads = conn.execute("SELECT count(*) FROM ads").fetchone()[0]
            pending = conn.execute("SELECT count(*) FROM ads WHERE status='pending'").fetchone()[0]
            reports = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
            open_reports = conn.execute("SELECT count(*) FROM reports WHERE status='open'").fetchone()[0]
            views = conn.execute("SELECT ad_views, started_at FROM marketplace_metrics WHERE id=1").fetchone()
        return web.json_response({"users": users, "ads": ads, "pending_ads": pending, "reports": reports,
                                  "open_reports": open_reports, "ad_views": views["ad_views"],
                                  "views_started_at": views["started_at"],
                                  "chat_review_access": request["user"]["id"] in config.chat_reviewer_ids})

    async def health(request):
        return web.json_response({"ok": True})

    from services.marketplace_web import register_marketplace_routes
    media_downloads = asyncio.Semaphore(4)

    async def download_photo(file_id):
        async with media_downloads:
            return await download_telegram_photo(application.bot, file_id)

    register_marketplace_routes(app, application, lock, download_photo)
    from services.banners import register_banner_routes
    register_banner_routes(app, application, normalize_photo)
    from services.admin_users import register_user_routes
    register_user_routes(app, application)
    from services.admin_settings import register_admin_settings_routes
    register_admin_settings_routes(app, application)
    from services.admin_market import register_admin_market_routes
    register_admin_market_routes(app, application, lock, download_photo)
    from services.chat import register_chat_routes
    register_chat_routes(app, application, normalize_photo, download_photo)
    app.router.add_get("/api/admin/summary", admin_summary)
    app.router.add_get("/health", health)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/extract", extract)
    app.router.add_put("/api/draft", save)
    app.router.add_post("/api/photo", photo)
    app.router.add_post("/api/photo/delete", delete_photo)
    app.router.add_get("/api/media/{file_id}", media)
    app.router.add_post("/api/submit", submit)
    app.router.add_get("/", static)
    app.router.add_get("/{name}", static)
    return app


async def start_server(application):
    config = application.bot_data["config"]
    if not config.mini_app_url:
        return
    runner = web.AppRunner(create_web_app(application), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", config.web_port).start()
    except Exception:
        await runner.cleanup()
        raise
    application.bot_data["miniapp_runner"] = runner
    logger.info("Mini App server started on configured port")


async def stop_server(application):
    runner = application.bot_data.pop("miniapp_runner", None)
    if runner:
        await runner.cleanup()
