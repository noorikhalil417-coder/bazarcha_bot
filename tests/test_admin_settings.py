import unittest
from dataclasses import replace

from aiohttp.test_utils import TestClient, TestServer

import test_miniapp as fixtures
from database import AFGHAN_PROVINCES
from services.miniapp import create_web_app


class AdminSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ApiTests.asyncSetUp(self)
        await self.client.close()
        self.config = replace(self.config, group_id=-1001)
        self.application.bot_data["config"] = self.config
        self.client = TestClient(TestServer(create_web_app(self.application)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await fixtures.ApiTests.asyncTearDown(self)

    def headers(self, uid=100):
        return fixtures.ApiTests.headers(self, uid)

    async def test_reference_permissions_validation_order_and_state(self):
        path = "/api/admin/reference/provinces"
        self.assertEqual((await self.client.get(path)).status, 401)
        self.assertEqual((await self.client.get(path, headers=self.headers())).status, 403)
        listing = await (await self.client.get(path, headers=self.headers(41))).json()
        self.assertEqual(len(listing["items"]), len(AFGHAN_PROVINCES))
        province = listing["items"][0]
        self.assertEqual((await self.client.put(path + f"/{province['province_id']}", json={"name": province["name"], "enabled": False}, headers=self.headers(41))).status, 200)
        state = await (await self.client.get("/api/state", headers=self.headers())).json()
        self.assertNotIn(province["name"], state["provinces"])
        self.assertEqual((await self.client.post(path, json={"name": "کابل", "enabled": True}, headers=self.headers(41))).status, 400)
        self.assertEqual((await self.client.get("/api/admin/reference/unknown", headers=self.headers(41))).status, 404)
        category_path = "/api/admin/reference/categories"
        result = await self.client.post(category_path, json={"name": "دسته آزمایشی", "enabled": True}, headers=self.headers(41))
        category_id = (await result.json())["id"]
        self.assertEqual((await self.client.post(category_path + f"/{category_id}/move", json={"direction":"up"}, headers=self.headers(41))).status, 200)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM admin_actions WHERE action IN ('categories_save','categories_reorder','provinces_save')").fetchone()[0], 3)

    async def test_settings_validate_and_write_existing_stores(self):
        path = "/api/admin/settings"
        self.assertEqual((await self.client.post(path, json={"key": "links_allowed", "value": False}, headers=self.headers())).status, 403)
        self.assertEqual((await self.client.post(path, json={"key": "links_allowed", "value": "0"}, headers=self.headers(41))).status, 400)
        self.assertEqual((await self.client.post(path, json={"key": "token", "value": "bad"}, headers=self.headers(41))).status, 400)
        self.assertEqual((await self.client.post(path, json={"key": "links_allowed", "value": False}, headers=self.headers(41))).status, 200)
        self.assertEqual((await self.client.post(path, json={"key": "invite_required", "value": True}, headers=self.headers(41))).status, 200)
        self.assertEqual((await self.client.post(path, json={"key": "required_invites", "value": 21}, headers=self.headers(41))).status, 400)
        self.assertEqual((await self.client.post(path, json={"key": "required_invites", "value": 5}, headers=self.headers(41))).status, 200)
        data = await (await self.client.get(path, headers=self.headers(41))).json()
        self.assertEqual(data["values"]["links_allowed"], "0")
        self.assertEqual(data["group"]["invite_required"], 1)
        self.assertEqual(data["group"]["required_invites"], 5)
