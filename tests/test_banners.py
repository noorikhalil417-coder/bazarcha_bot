import base64
import io
import time
import unittest

from PIL import Image

import test_miniapp as fixtures


class BannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ApiTests.asyncSetUp(self)
        data = io.BytesIO()
        Image.new("RGB", (20, 10), "blue").save(data, "PNG")
        self.payload = {
            "title": "تبلیغ آزمایشی", "placement": "home", "url": "https://example.com/",
            "enabled": True, "starts_at": None, "ends_at": None,
            "image": base64.b64encode(data.getvalue()).decode(),
        }

    async def asyncTearDown(self):
        await fixtures.ApiTests.asyncTearDown(self)

    def headers(self, uid=100):
        return fixtures.ApiTests.headers(self, uid)

    async def post_banner(self, payload=None, uid=41):
        return await self.client.post("/api/admin/banners", json=payload or self.payload, headers=self.headers(uid))

    async def test_admin_authorization_input_validation_and_audit(self):
        self.assertEqual((await self.client.post("/api/admin/banners", json=self.payload)).status, 401)
        self.assertEqual((await self.post_banner(uid=100)).status, 403)
        for changes in ({"url": "javascript:alert(1)"}, {"image": "not-an-image"},
                        {"starts_at": 20, "ends_at": 10}, {"enabled": "true"}, {"placement": "other"}):
            with self.subTest(changes=changes):
                self.assertEqual((await self.post_banner({**self.payload, **changes})).status, 400)
        created = await self.post_banner()
        self.assertEqual(created.status, 200)
        banner_id = (await created.json())["banner_id"]
        response = await self.client.get(f"/api/banners/{banner_id}/image", headers=self.headers(100))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/jpeg")
        self.assertEqual((await self.client.delete(f"/api/admin/banners/{banner_id}", json={}, headers=self.headers(41))).status, 200)
        self.assertEqual((await self.client.get(f"/api/banners/{banner_id}/image", headers=self.headers(100))).status, 404)
        with self.db.connect() as conn:
            actions = [r[0] for r in conn.execute("SELECT action FROM admin_actions ORDER BY action_id")]
        self.assertEqual(actions, ["banner_create", "banner_delete"])

    async def test_scheduling_limit_edit_and_reordering(self):
        now = int(time.time())
        ids = []
        for i in range(7):
            response = await self.post_banner({**self.payload, "title": str(i)})
            self.assertEqual(response.status, 200)
            ids.append((await response.json())["banner_id"])
        self.assertEqual((await self.post_banner()).status, 400)
        second_slot = await self.post_banner({**self.payload, "placement": "browse", "starts_at": now + 3600})
        future = (await second_slot.json())["banner_id"]
        self.assertEqual((await (await self.client.get("/api/banners?placement=browse", headers=self.headers())).json())["items"], [])
        self.assertEqual((await self.client.get(f"/api/banners/{future}/image", headers=self.headers())).status, 404)
        self.assertEqual((await self.client.get(f"/api/admin/banners/{future}/image", headers=self.headers(41))).status, 200)
        self.assertEqual((await self.client.post(f"/api/admin/banners/{ids[1]}/move", json={"direction": "up"}, headers=self.headers(41))).status, 200)
        public = await (await self.client.get("/api/banners?placement=home", headers=self.headers())).json()
        self.assertEqual([x["banner_id"] for x in public["items"]][:2], [ids[1], ids[0]])
        updated = {**self.payload, "enabled": False, "image": ""}
        self.assertEqual((await self.client.put(f"/api/admin/banners/{ids[0]}", json=updated, headers=self.headers(41))).status, 200)
        self.assertEqual((await self.client.get(f"/api/banners/{ids[0]}/image", headers=self.headers())).status, 404)
        self.assertEqual((await self.post_banner()).status, 200)

    async def test_nonoverlapping_scheduled_banners_do_not_exceed_active_limit(self):
        for _ in range(7):
            self.assertEqual((await self.post_banner({**self.payload, "starts_at": 1000, "ends_at": 2000})).status, 200)
        self.assertEqual((await self.post_banner({**self.payload, "starts_at": 2000, "ends_at": 3000})).status, 200)
        self.assertEqual((await self.post_banner({**self.payload, "starts_at": 1999, "ends_at": 3000})).status, 400)
