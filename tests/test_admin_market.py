import unittest
from unittest.mock import AsyncMock

from telegram.error import Forbidden

import test_miniapp as fixtures
import test_marketplace_web as market_fixtures


class AdminMarketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ApiTests.asyncSetUp(self)
        self.bot.edit_message_text = AsyncMock()
        self.bot.edit_message_caption = AsyncMock()

    async def asyncTearDown(self):
        await fixtures.ApiTests.asyncTearDown(self)

    def headers(self, uid=100):
        return fixtures.ApiTests.headers(self, uid)

    async def put(self, body=None, uid=100):
        return await fixtures.ApiTests.put(self, body, uid)

    async def publish_ad(self, **kwargs):
        return await market_fixtures.MarketplaceWebTests.publish_ad(self, **kwargs)

    async def action(self, ad_id, name):
        return await self.client.post(f"/api/admin/ads/{ad_id}/action", json={"action": name}, headers=self.headers(41))

    async def test_admin_only_and_published_archive_restore_preserves_history(self):
        for path in ("/api/admin/ads", "/api/admin/reports", "/api/admin/history"):
            self.assertEqual((await self.client.get(path)).status, 401)
            self.assertEqual((await self.client.get(path, headers=self.headers())).status, 403)
        ad_id = await self.publish_ad(with_photo=True)
        photos = self.market.photos(ad_id)
        self.assertEqual((await self.client.get(f"/api/admin/ads/{ad_id}/photo/0", headers=self.headers())).status, 403)
        self.assertEqual((await self.client.get(f"/api/admin/ads/{ad_id}/photo/0", headers=self.headers(41))).status, 200)
        self.assertEqual((await self.action(ad_id, "archive")).status, 200)
        self.assertEqual(self.market.get_ad(ad_id)["status"], "archived")
        self.assertEqual((await self.action(ad_id, "restore")).status, 200)
        ad = self.market.get_ad(ad_id)
        self.assertEqual(ad["status"], "draft")
        self.assertIsNone(ad["publication_message_id"])
        self.assertEqual(self.market.photos(ad_id), photos)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT message_id FROM ad_publication_history WHERE ad_id=?", (ad_id,)).fetchone()[0], 700 + ad_id)
        self.market.submit(ad_id, 100)
        self.market.transition(ad_id, "approved", 41)
        self.assertTrue(self.market.claim_publication(ad_id, 41, -1001234567890))

    async def test_group_failure_is_visible_and_blocks_unsafe_restore(self):
        ad_id = await self.publish_ad()
        self.bot.edit_message_text.side_effect = Forbidden("missing rights")
        response = await self.action(ad_id, "archive")
        self.assertEqual(response.status, 502)
        self.assertEqual(self.market.get_ad(ad_id)["status"], "archived")
        self.assertEqual((await self.action(ad_id, "restore")).status, 502)
        self.assertEqual(self.market.get_ad(ad_id)["status"], "archived")
        self.bot.edit_message_text.side_effect = None
        self.assertEqual((await self.action(ad_id, "sync")).status, 200)
        self.assertEqual((await self.action(ad_id, "restore")).status, 200)

    async def test_feature_and_report_actions_audited(self):
        ad_id = await self.publish_ad()
        self.market.ensure_user(200, "reporter", "Reporter")
        report_id = self.market.add_report(ad_id, 200, "other")
        featured = f"/api/admin/ads/{ad_id}/featured"
        self.assertEqual((await self.client.post(featured, json={"enabled": True, "days": 0}, headers=self.headers(41))).status, 400)
        self.assertEqual((await self.client.post(featured, json={"enabled": True, "days": 7}, headers=self.headers(41))).status, 200)
        self.assertIsNotNone(self.market.get_ad(ad_id)["featured_until"])
        self.assertEqual((await self.client.post(f"/api/admin/reports/{report_id}", json={"action":"review"}, headers=self.headers(41))).status, 200)
        self.assertEqual((await self.client.post(f"/api/admin/reports/{report_id}", json={"action":"review"}, headers=self.headers(41))).status, 400)
        history = await (await self.client.get("/api/admin/history", headers=self.headers(41))).json()
        self.assertTrue({"featured_set", "report_reviewed"}.issubset({item["action"] for item in history["items"]}))

    async def test_v8_migration_preserves_existing_marketplace_data(self):
        ad_id = await self.publish_ad(with_photo=True)
        photos = self.market.photos(ad_id)
        self.market.set_setting("welcome_message", "پیام قدیمی", 41)
        with self.db.transaction() as conn:
            for table in ("ad_publication_history", "provinces", "blocked_users", "banners"):
                conn.execute(f"DROP TABLE {table}")
            conn.execute("PRAGMA user_version=8")
        self.db.initialize()
        self.db.initialize()
        self.assertEqual(self.market.get_ad(ad_id)["status"], "published")
        self.assertEqual(self.market.photos(ad_id), photos)
        self.assertEqual(self.market.get_setting("welcome_message"), "پیام قدیمی")
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM provinces").fetchone()[0], 34)
            self.assertEqual(conn.execute("SELECT count(*) FROM banners").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
