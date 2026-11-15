import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest
from telegram.ext import Application, ApplicationHandlerStop

from config import Config
from database import Database
from handlers.admin import _dashboard_callback, admin_panel
from handlers.group_gate import admin_gate_callback
from handlers.panel import reject_stale_panel_callback, render
from handlers.user import my_ads, my_ads_callback, navigation, start
from services.group_policy import GroupPolicy
from services.marketplace import Marketplace


class FakeBot:
    def __init__(self):
        self.messages = {}
        self.next_id = 1
        self.error = None
        self.id = 999
        self.get_chat_member = AsyncMock(return_value=SimpleNamespace(
            status=ChatMemberStatus.ADMINISTRATOR,
            can_delete_messages=True,
            can_invite_users=True,
        ))

    async def send_message(self, chat_id, text, **kwargs):
        message_id = self.next_id
        self.next_id += 1
        self.messages[chat_id, message_id] = (text, kwargs.get("reply_markup"))
        return SimpleNamespace(message_id=message_id)

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        if self.error:
            raise self.error
        key = (chat_id, message_id)
        if key not in self.messages:
            raise BadRequest("Message to edit not found")
        value = (text, kwargs.get("reply_markup"))
        if self.messages[key] == value:
            raise BadRequest("Message is not modified")
        self.messages[key] = value
        return SimpleNamespace(message_id=message_id)


class PrivatePanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "data.sqlite3")
        self.db.initialize()
        self.market = Marketplace(self.db)
        self.policy = GroupPolicy(self.db)
        self.bot = FakeBot()
        self.config = Config.from_env({
            "TELEGRAM_BOT_TOKEN": "12345:test",
            "ADMIN_IDS": "41",
            "GROUP_ID": "-1001",
        })
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "database": self.db, "marketplace": self.market,
                "group_policy": self.policy, "config": self.config,
            }),
            bot=self.bot, args=[], user_data={},
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def update(self, user_id=41, data=None, message_id=None):
        user = SimpleNamespace(id=user_id, username=None, full_name="User")
        chat = SimpleNamespace(id=user_id, type="private")
        query = None
        message = SimpleNamespace(message_id=message_id, reply_text=AsyncMock())
        if data:
            async def edit(text, **kwargs):
                return await self.bot.edit_message_text(chat.id, message_id, text, **kwargs)
            query = SimpleNamespace(
                from_user=user, data=data, message=message, answer=AsyncMock(),
                edit_message_text=AsyncMock(side_effect=edit),
            )
        return SimpleNamespace(
            effective_user=user, effective_chat=chat,
            effective_message=message, callback_query=query,
        )

    async def click(self, data, handler, user_id=41, message_id=1):
        update = self.update(user_id, data, message_id)
        await reject_stale_panel_callback(update, self.context)
        await handler(update, self.context)
        return update

    async def test_commands_reuse_panel_deleted_panel_is_replaced_and_users_are_isolated(self):
        await start(self.update(), self.context)
        await admin_panel(self.update(), self.context)
        await admin_panel(self.update(), self.context)
        self.context.application.bot_data["database"] = Database(self.db.path)
        await start(self.update(), self.context)
        self.assertEqual(self.bot.next_id, 2)
        await start(self.update(100), self.context)
        self.assertEqual(self.bot.next_id, 3)
        del self.bot.messages[41, 1]
        await start(self.update(), self.context)
        await admin_panel(self.update(), self.context)
        self.assertEqual(self.bot.next_id, 4)
        with self.db.connect() as conn:
            rows = conn.execute("SELECT chat_id,message_id FROM private_panels ORDER BY chat_id").fetchall()
        self.assertEqual([tuple(row) for row in rows], [(41, 3), (100, 2)])
        stale = self.update(41, "menu:home", 1)
        with self.assertRaises(ApplicationHandlerStop):
            await reject_stale_panel_callback(stale, self.context)
        stale.callback_query.answer.assert_awaited_once()
        with self.assertRaises(ApplicationHandlerStop):
            await reject_stale_panel_callback(self.update(41, "ad:approve:7", 1), self.context)
        self.bot.error = BadRequest("Bad request: unrelated problem")
        with self.assertRaises(BadRequest):
            await start(self.update(), self.context)
        self.assertEqual(self.bot.next_id, 4)

    async def test_unrelated_notification_is_not_replaced_or_blocked(self):
        await start(self.update(), self.context)
        self.bot.messages[41, 99] = ("آگهی در انتظار بررسی", None)
        notification = self.update(41, "ad:approve:7", 99)
        await reject_stale_panel_callback(notification, self.context)
        await render(notification, self.context, "بررسی شد")
        self.assertEqual(self.bot.messages[41, 99][0], "بررسی شد")
        self.assertIn("بازارچه خوش آمدید", self.bot.messages[41, 1][0])
        self.assertEqual(self.bot.next_id, 2)

    async def test_rapid_callbacks_are_serialized_and_stale_toggle_is_ignored(self):
        await admin_panel(self.update(), self.context)
        await self.click("admin:group_gate", _dashboard_callback)
        processor = Application.builder().token("12345:test").concurrent_updates(False).build().update_processor

        async def run_click(data):
            update = self.update(41, data, 1)
            try:
                await reject_stale_panel_callback(update, self.context)
            except ApplicationHandlerStop:
                return
            await admin_gate_callback(update, self.context)

        await asyncio.gather(*(
            processor.process_update(None, run_click("gate:invite_required:on"))
            for _ in range(10)
        ))
        self.assertTrue(self.policy.settings(-1001)["invite_required"])
        with self.db.connect() as conn:
            count = conn.execute("SELECT count(*) FROM admin_actions WHERE action='group_policy'").fetchone()[0]
        self.assertEqual(count, 1)
        await asyncio.gather(*(
            processor.process_update(None, run_click("gate:panel"))
            for _ in range(10)
        ))
        self.assertEqual(self.bot.next_id, 2)

    async def test_refresh_toggle_and_stale_clicks_do_not_change_settings_or_create_messages(self):
        await admin_panel(self.update(), self.context)
        await self.click("admin:group_gate", _dashboard_callback)
        for _ in range(3):
            update = await self.click("gate:panel", admin_gate_callback)
            update.callback_query.answer.assert_awaited_once()
        await self.click("gate:invite_required:on", admin_gate_callback)
        self.assertTrue(self.policy.settings(-1001)["invite_required"])
        stale = self.update(41, "gate:invite_required:on", 1)
        with self.assertRaises(ApplicationHandlerStop):
            await reject_stale_panel_callback(stale, self.context)
        await self.click("gate:invite_required:off", admin_gate_callback)
        await self.click("gate:ad_delete:on", admin_gate_callback)
        await self.click("gate:ad_delete:off", admin_gate_callback)
        await self.click("admin:home", _dashboard_callback)
        stale = self.update(41, "gate:panel", 1)
        with self.assertRaises(ApplicationHandlerStop):
            await reject_stale_panel_callback(stale, self.context)
        self.assertEqual(self.bot.next_id, 2)
        self.assertFalse(self.policy.settings(-1001)["invite_required"])
        self.assertFalse(self.policy.settings(-1001)["ad_delete"])

    async def test_paginated_ads_details_back_and_owner_isolation(self):
        self.market.ensure_user(41)
        self.market.ensure_user(100)
        ids = [self.market.create_draft(41, 1, "sale") for _ in range(12)]
        other = self.market.create_draft(100, 1, "sale")
        await my_ads(self.update(), self.context)
        text, keyboard = self.bot.messages[41, 1]
        self.assertIn("صفحهٔ 1", text)
        self.assertEqual(sum(row[0].callback_data.startswith("own:detail") for row in keyboard.inline_keyboard), 5)
        await self.click("own:list:1", my_ads_callback)
        text, keyboard = self.bot.messages[41, 1]
        self.assertIn("صفحهٔ 2", text)
        stale = self.update(41, "own:list:1", 1)
        with self.assertRaises(ApplicationHandlerStop):
            await reject_stale_panel_callback(stale, self.context)
        await self.click(f"own:detail:{ids[6]}:1", my_ads_callback)
        self.assertIn(f"#{ids[6]}", self.bot.messages[41, 1][0])
        await self.click("own:list:1", my_ads_callback)
        await self.click("menu:home", navigation)
        self.assertEqual(self.bot.next_id, 2)
        self.assertIsNone(self.market.owned_ad(other, 41))

    async def test_migration_preserves_existing_ads_and_active_group_policies(self):
        self.market.ensure_user(41)
        ad_id = self.market.create_draft(41, 1, "sale")
        self.policy.set_enabled(-1001, "invite_required", True, 41)
        self.policy.set_enabled(-1001, "ad_delete", True, 41)
        with self.db.transaction() as conn:
            conn.execute("DROP TABLE private_panels")
            conn.execute("PRAGMA user_version=6")
        self.db.initialize()
        self.assertIsNotNone(self.market.owned_ad(ad_id, 41))
        self.assertTrue(self.policy.settings(-1001)["invite_required"])
        self.assertTrue(self.policy.settings(-1001)["ad_delete"])
