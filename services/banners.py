"""Scheduled, administrator-managed Mini App banners."""

import time
from urllib.parse import urlsplit

from aiohttp import web

from services.marketplace import MarketplaceError


def register_banner_routes(app, application, normalize_photo):
    db = application.bot_data["marketplace"].db

    def fields(body):
        placement = body.get("placement")
        title = body.get("title")
        url = body.get("url", "")
        enabled = body.get("enabled")
        if placement not in {"home", "browse"} or not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
            raise MarketplaceError("عنوان یا جایگاه بنر معتبر نیست.")
        if type(enabled) is not bool or not isinstance(url, str) or len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
            raise MarketplaceError("وضعیت یا پیوند بنر معتبر نیست.")
        if url:
            try:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError
                _ = parsed.port
            except ValueError as exc:
                raise MarketplaceError("پیوند باید یک نشانی معتبر HTTPS باشد.") from exc
        start, end = body.get("starts_at"), body.get("ends_at")
        for value in (start, end):
            if value is not None and (type(value) is not int or not 0 <= value <= 253402300799):
                raise MarketplaceError("زمان بنر معتبر نیست.")
        if start is not None and end is not None and start >= end:
            raise MarketplaceError("پایان بنر باید پس از شروع باشد.")
        return placement, title.strip(), url, start, end, int(enabled)

    def audit(conn, uid, action, target):
        conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)", (uid, action, str(target)))

    async def admin_list(request):
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT banner_id,placement,title,url,starts_at,ends_at,enabled,sort_order "
                "FROM banners ORDER BY placement,sort_order,banner_id"
            ).fetchall()
        return web.json_response({"items": [dict(row) for row in rows]})

    async def save(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
        values = fields(body)
        banner_id = int(request.match_info["banner_id"]) if "banner_id" in request.match_info else None
        image = normalize_photo(body["image"]) if body.get("image") else None
        uid = request["user"]["id"]
        with db.transaction() as conn:
            old = conn.execute("SELECT * FROM banners WHERE banner_id=?", (banner_id,)).fetchone() if banner_id else None
            if banner_id and old is None:
                raise web.HTTPNotFound()
            if not image and old is None:
                raise MarketplaceError("تصویر بنر لازم است.")
            if values[5]:
                rows = conn.execute(
                    "SELECT starts_at,ends_at FROM banners WHERE placement=? AND enabled=1 AND banner_id!=?",
                    (values[0], banner_id or 0),
                ).fetchall()
                intervals = [(row["starts_at"], row["ends_at"]) for row in rows] + [(values[3], values[4])]
                points = []
                for start, end in intervals:
                    points.append((start if start is not None else -1, 1))
                    points.append((end if end is not None else 253402300800, -1))
                active = 0
                for _, change in sorted(points):
                    active += change
                    if active > 7:
                        raise MarketplaceError("در هر زمان و جایگاه حداکثر هفت بنر فعال مجاز است.")
            if old:
                conn.execute(
                    "UPDATE banners SET placement=?,title=?,url=?,starts_at=?,ends_at=?,enabled=?,image=? WHERE banner_id=?",
                    (*values, image if image is not None else old["image"], banner_id),
                )
                action = "banner_update"
            else:
                order = conn.execute("SELECT coalesce(max(sort_order),-1)+1 FROM banners WHERE placement=?", (values[0],)).fetchone()[0]
                cursor = conn.execute(
                    "INSERT INTO banners(placement,title,url,starts_at,ends_at,enabled,image,sort_order) VALUES(?,?,?,?,?,?,?,?)",
                    (*values, image, order),
                )
                banner_id = cursor.lastrowid
                action = "banner_create"
            audit(conn, uid, action, banner_id)
        return web.json_response({"banner_id": banner_id})

    async def remove(request):
        banner_id = int(request.match_info["banner_id"])
        with db.transaction() as conn:
            if conn.execute("DELETE FROM banners WHERE banner_id=?", (banner_id,)).rowcount != 1:
                raise web.HTTPNotFound()
            audit(conn, request["user"]["id"], "banner_delete", banner_id)
        return web.json_response({"ok": True})

    async def move(request):
        banner_id = int(request.match_info["banner_id"])
        body = await request.json()
        if not isinstance(body, dict) or body.get("direction") not in {"up", "down"}:
            raise MarketplaceError("جهت تغییر ترتیب معتبر نیست.")
        with db.transaction() as conn:
            row = conn.execute("SELECT placement FROM banners WHERE banner_id=?", (banner_id,)).fetchone()
            if not row:
                raise web.HTTPNotFound()
            ids = [r[0] for r in conn.execute(
                "SELECT banner_id FROM banners WHERE placement=? ORDER BY sort_order,banner_id", (row["placement"],)
            )]
            index = ids.index(banner_id)
            other = index + (-1 if body["direction"] == "up" else 1)
            if not 0 <= other < len(ids):
                raise MarketplaceError("بنر در انتهای فهرست است.")
            ids[index], ids[other] = ids[other], ids[index]
            conn.executemany("UPDATE banners SET sort_order=? WHERE banner_id=?", enumerate(ids))
            audit(conn, request["user"]["id"], "banner_reorder", banner_id)
        return web.json_response({"ok": True})

    async def public_list(request):
        placement = request.query.get("placement")
        if placement not in {"home", "browse"}:
            raise MarketplaceError("جایگاه معتبر نیست.")
        now = int(time.time())
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT banner_id,title,url,ends_at FROM banners WHERE placement=? AND enabled=1 "
                "AND (starts_at IS NULL OR starts_at<=?) AND (ends_at IS NULL OR ends_at>?) "
                "ORDER BY sort_order,banner_id", (placement, now, now),
            ).fetchall()
        return web.json_response({"items": [dict(row) for row in rows]})

    async def image(request):
        banner_id = int(request.match_info["banner_id"])
        admin = request.path.startswith("/api/admin/")
        now = int(time.time())
        with db.connect() as conn:
            if admin:
                row = conn.execute("SELECT image FROM banners WHERE banner_id=?", (banner_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT image FROM banners WHERE banner_id=? AND enabled=1 "
                    "AND (starts_at IS NULL OR starts_at<=?) AND (ends_at IS NULL OR ends_at>?)",
                    (banner_id, now, now),
                ).fetchone()
        if not row:
            raise web.HTTPNotFound()
        return web.Response(body=row["image"], content_type="image/jpeg")

    app.router.add_get("/api/admin/banners", admin_list)
    app.router.add_post("/api/admin/banners", save)
    app.router.add_put("/api/admin/banners/{banner_id}", save)
    app.router.add_delete("/api/admin/banners/{banner_id}", remove)
    app.router.add_post("/api/admin/banners/{banner_id}/move", move)
    app.router.add_get("/api/admin/banners/{banner_id}/image", image)
    app.router.add_get("/api/banners", public_list)
    app.router.add_get("/api/banners/{banner_id}/image", image)
