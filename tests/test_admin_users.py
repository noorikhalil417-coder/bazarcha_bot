import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from telegram.error import Forbidden
from telegram.ext import ApplicationHandlerStop

import test_miniapp as fixtures
from services.admin_users import blocked_user_guard
from services.miniapp import create_web_app
from handlers.admin import unban_user


class AdminUsersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ApiTests.asyncSetUp(self)
        await self.client.close()
        self.config = replace(self.config, group_id=-1001)
        self.application.bot_data["config"] = self.config
        self.application.bot_data["database"] = self.db
        self.bot.ban_chat_member = AsyncMock()
        self.bot.unban_chat_member = AsyncMock()
        self.client = TestClient(TestServer(create_web_app(self.application)))
        await self.client.start_server()
        self.market.ensure_user(100, "test_user", "Test")

    async def asyncTearDown(self):
        await fixtures.ApiTests.asyncTearDown(self)

    def headers(self, uid=100):
        return fixtures.ApiTests.headers(self, uid)

    async def block(self, uid, blocked=True, reason="دلیل"):
        return await self.client.post(f"/api/admin/users/{uid}/block",
                                      json={"blocked": blocked, "reason": reason}, headers=self.headers(41))

    async def test_block_enforced_and_unblock_restores_access(self):
        self.assertEqual((await self.client.post("/api/admin/users/100/block", json={"blocked": True, "reason": "x"}, headers=self.headers(100))).status, 403)
        self.assertEqual((await self.block(100)).status, 200)
        self.bot.ban_chat_member.assert_awaited_once_with(-1001, 100)
        self.assertEqual((await self.client.get("/api/state", headers=self.headers(100))).status, 403)
        message = SimpleNamespace(text="/start", reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=100), effective_chat=SimpleNamespace(type="private"),
                                 callback_query=None, effective_message=message)
        context = SimpleNamespace(application=self.application)
        with self.assertRaises(ApplicationHandlerStop):
            await blocked_user_guard(update, context)
        message.reply_text.assert_awaited_once()
        self.assertEqual((await self.block(100, False)).status, 200)
        self.bot.unban_chat_member.assert_awaited_once_with(-1001, 100, only_if_banned=True)
        self.assertEqual((await self.client.get("/api/state", headers=self.headers(100))).status, 200)

    async def test_group_failure_is_reported_and_local_access_stays_blocked(self):
        self.bot.ban_chat_member.side_effect = Forbidden("missing rights")
        response = await self.block(100)
        self.assertEqual(response.status, 502)
        self.assertTrue((await response.json())["local_blocked"])
        self.assertEqual((await self.client.get("/api/state", headers=self.headers())).status, 403)
        self.bot.ban_chat_member.side_effect = None
        self.assertEqual((await self.block(100)).status, 200)
        self.bot.unban_chat_member.side_effect = Forbidden("missing rights")
        self.assertEqual((await self.block(100, False)).status, 502)
        self.assertEqual((await self.client.get("/api/state", headers=self.headers())).status, 403)
        with self.db.connect() as conn:
            actions = [row[0] for row in conn.execute("SELECT action FROM admin_actions ORDER BY action_id")]
        self.assertIn("web_group_ban_failed", actions)
        self.assertIn("web_group_unban_failed", actions)

    async def test_admin_and_missing_users_cannot_be_blocked(self):
        self.assertEqual((await self.block(41)).status, 400)
        self.assertEqual((await self.block(999)).status, 404)
        self.assertEqual((await self.block(100, True, "")).status, 400)
        self.bot.ban_chat_member.assert_not_awaited()

    async def test_legacy_unban_cannot_bypass_web_block(self):
        self.assertEqual((await self.block(100)).status, 200)
        message = SimpleNamespace(reply_text=AsyncMock(), reply_to_message=None)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=41), effective_message=message)
        context = SimpleNamespace(application=self.application, args=["100"], bot=self.bot)
        await unban_user(update, context)
        self.bot.unban_chat_member.assert_not_awaited()
        message.reply_text.assert_awaited_once()

    async def test_legacy_unban_waits_for_inflight_web_block(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def ban(*args):
            started.set()
            await release.wait()
        self.bot.ban_chat_member.side_effect = ban
        message = SimpleNamespace(reply_text=AsyncMock(), reply_to_message=None)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=41), effective_message=message)
        context = SimpleNamespace(application=self.application, args=["100"], bot=self.bot)
        blocking = asyncio.create_task(self.block(100))
        await asyncio.wait_for(started.wait(), 2)
        unblocking = asyncio.create_task(unban_user(update, context))
        await asyncio.sleep(.05)
        self.assertFalse(unblocking.done())
        release.set()
        self.assertEqual((await blocking).status, 200)
        await unblocking
        self.bot.unban_chat_member.assert_not_awaited()
