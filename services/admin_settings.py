"""Administrative reference data and existing bot/group settings."""

import sqlite3

from aiohttp import web

from services.group_policy import GroupPolicy
from services.marketplace import MarketplaceError, normalize_text


def register_admin_settings_routes(app, application):
    market = application.bot_data["marketplace"]
    db = market.db
    config = application.bot_data["config"]
    policy = GroupPolicy(db)
    references = {"categories": ("categories", "category_id"), "provinces": ("provinces", "province_id")}
    text_defaults = {"rules": config.group_rules, "welcome_message": config.welcome_message, "blocked_phrases": ""}
    flag_defaults = {"welcome_enabled": "1", "links_allowed": "1", "join_approval": "0", "public_rankings": "1"}

    def reference(request):
        kind = request.match_info["kind"]
        if kind not in references:
            raise web.HTTPNotFound()
        return kind, *references[kind]

    async def list_reference(request):
        _, table, column = reference(request)
        with db.connect() as conn:
            rows = conn.execute(f"SELECT {column},name,enabled,sort_order FROM {table} ORDER BY sort_order,{column}").fetchall()
        return web.json_response({"items": [dict(row) for row in rows]})

    async def save_reference(request):
        kind, table, column = reference(request)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("name"), str) or type(body.get("enabled")) is not bool:
            raise MarketplaceError("نام یا وضعیت معتبر نیست.")
        name = normalize_text(body["name"])
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise MarketplaceError("نام باید میان ۱ تا ۸۰ نویسه باشد.")
        ident = int(request.match_info["id"]) if "id" in request.match_info else None
        try:
            with db.transaction() as conn:
                if ident:
                    changed = conn.execute(f"UPDATE {table} SET name=?,enabled=? WHERE {column}=?",
                                           (name, int(body["enabled"]), ident)).rowcount
                    if not changed:
                        raise web.HTTPNotFound()
                else:
                    order = conn.execute(f"SELECT coalesce(max(sort_order),-1)+1 FROM {table}").fetchone()[0]
                    ident = conn.execute(f"INSERT INTO {table}(name,enabled,sort_order) VALUES(?,?,?)",
                                         (name, int(body["enabled"]), order)).lastrowid
                conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)",
                             (request["user"]["id"], f"{kind}_save", str(ident)))
        except sqlite3.IntegrityError as exc:
            raise MarketplaceError("این نام از پیش ثبت شده است.") from exc
        return web.json_response({"id": ident})

    async def move_reference(request):
        kind, table, column = reference(request)
        ident = int(request.match_info["id"])
        body = await request.json()
        if not isinstance(body, dict) or body.get("direction") not in {"up", "down"}:
            raise MarketplaceError("جهت معتبر نیست.")
        with db.transaction() as conn:
            ids = [row[0] for row in conn.execute(f"SELECT {column} FROM {table} ORDER BY sort_order,{column}")]
            if ident not in ids:
                raise web.HTTPNotFound()
            index = ids.index(ident)
            other = index + (-1 if body["direction"] == "up" else 1)
            if not 0 <= other < len(ids):
                raise MarketplaceError("مورد در انتهای فهرست است.")
            ids[index], ids[other] = ids[other], ids[index]
            conn.executemany(f"UPDATE {table} SET sort_order=? WHERE {column}=?", enumerate(ids))
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)",
                         (request["user"]["id"], f"{kind}_reorder", str(ident)))
        return web.json_response({"ok": True})

    async def settings(request):
        values = {key: market.get_setting(key, default) for key, default in {**text_defaults, **flag_defaults}.items()}
        group = policy.settings(config.group_id) if config.group_id is not None else None
        return web.json_response({"values": values, "group": group,
                                  "publication_configured": config.publication_chat_id is not None})

    async def save_setting(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise MarketplaceError("تنظیم معتبر نیست.")
        key, value = body.get("key"), body.get("value")
        admin = request["user"]["id"]
        if key in flag_defaults:
            if type(value) is not bool:
                raise MarketplaceError("وضعیت معتبر نیست.")
            market.set_setting(key, "1" if value else "0", admin)
        elif key in text_defaults:
            if not isinstance(value, str) or len(value) > 3000 or (key != "blocked_phrases" and not value.strip()):
                raise MarketplaceError("متن تنظیم معتبر نیست.")
            market.set_setting(key, value.strip(), admin)
        elif key in {"invite_required", "ad_delete"}:
            if config.group_id is None or type(value) is not bool:
                raise MarketplaceError("گروه یا وضعیت معتبر نیست.")
            policy.set_enabled(config.group_id, key, value, admin)
        elif key == "required_invites":
            if config.group_id is None or type(value) is not int or not 1 <= value <= 20:
                raise MarketplaceError("شمار دعوت باید از ۱ تا ۲۰ باشد و گروه تنظیم شده باشد.")
            policy.set_required_invites(config.group_id, value, admin)
        else:
            raise MarketplaceError("این تنظیم قابل تغییر نیست.")
        return web.json_response({"ok": True})

    app.router.add_get("/api/admin/reference/{kind}", list_reference)
    app.router.add_post("/api/admin/reference/{kind}", save_reference)
    app.router.add_put("/api/admin/reference/{kind}/{id}", save_reference)
    app.router.add_post("/api/admin/reference/{kind}/{id}/move", move_reference)
    app.router.add_get("/api/admin/settings", settings)
    app.router.add_post("/api/admin/settings", save_setting)
