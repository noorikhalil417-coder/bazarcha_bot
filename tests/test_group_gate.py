from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop

from config import Config
from database import Database, SCHEMA_VERSION
from services.backup import create_backup
from services.group_policy import GroupPolicy, classify_group_ad
from services.marketplace import Marketplace
from handlers.group_gate import (
    enforce_group_message,
    invite_status,
    membership_update,
    admin_gate_callback,
)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "group.sqlite3")
        self.db.initialize()
        self.p = GroupPolicy(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_distinct_invites_survive_restart_backup_and_do_not_transfer(self):
        self.p.save_link(-1001, 100, "https://t.me/+one")
        self.p.save_link(-1001, 200, "https://t.me/+two")
        for uid in (201, 202, 203):
            self.assertEqual(self.p.register_join(-1001, uid, "https://t.me/+one"), 100)
        self.assertEqual(self.p.count(-1001, 100), 3)
        self.assertIsNone(self.p.register_join(-1001, 201, "https://t.me/+two"))
        self.assertIsNone(self.p.register_join(-1001, 100, "https://t.me/+one"))
        self.assertEqual(self.p.count(-1001, 200), 0)
        backup = create_backup(self.db, Path(self.tmp.name) / "backups")
        restored = Database(backup)
        restored.initialize()
        self.assertEqual(GroupPolicy(restored).count(-1001, 100), 3)
        self.assertEqual(GroupPolicy(restored).link(-1001, 100), "https://t.me/+one")

    def test_known_members_untracked_links_and_pending_requests(self):
        self.p.save_link(-1001, 100, "https://t.me/+one")
        self.p.seen(-1001, 201)
        self.assertIsNone(self.p.register_join(-1001, 201, "https://t.me/+one"))
        self.assertIsNone(self.p.register_join(-1001, 202, "https://t.me/+unknown"))
        self.assertEqual(self.p.count(-1001, 100), 0)
        self.p.pending_join(-1001, 203, "https://t.me/+one")
        self.assertEqual(
            self.p.count(-1001, 100), 0
        )  # Request alone is not membership.
        self.assertEqual(self.p.register_join(-1001, 203, None, True), 100)
        self.p.pending_join(-1001, 204, "https://t.me/+one")
        self.assertIsNone(self.p.register_join(-1001, 204, None, False))

    def test_message_and_join_race_uses_event_time(self):
        now = int(time.time())
        self.p.save_link(-1001, 100, "https://t.me/+one")
        self.p.seen(-1001, 201)
        self.assertEqual(
            self.p.register_join(-1001, 201, "https://t.me/+one", event_at=now), 100
        )
        self.assertIsNone(
            self.p.register_join(-1001, 201, "https://t.me/+one", event_at=now)
        )
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO group_members_seen VALUES(?,?,?)", (-1001, 202, now - 20)
            )
        self.assertIsNone(
            self.p.register_join(-1001, 202, "https://t.me/+one", event_at=now)
        )

    def test_default_off_and_cooldown_are_persistent(self):
        self.assertEqual(self.p.settings(-1001)["invite_required"], 0)
        self.assertEqual(self.p.settings(-1001)["ad_delete"], 0)
        self.assertTrue(self.p.claim_notice(-1001, 100))
        self.assertFalse(self.p.claim_notice(-1001, 100))
        self.assertTrue(self.p.claim_notice(-1001, 200))
        self.p.record_delete(-1001, 50, 100, "direct_ad")
        self.assertTrue(self.p.deleted(-1001, 50))
        self.assertFalse(self.p.deleted(-1002, 50))

    def test_conservative_classification_with_persian_numbers_and_service_offers(self):
        for text in (
            "گوشی سامسونگ فروشی، ۳۰۰۰ افغانی، مزارشریف",
            "یک گوشی سامسونگ در قیمت ۴هزارافغانی میخاستم در شهر مزارشریف ویا کابل",
            "برقکار هستم برای کار تماس بگیرید 0700000000",
        ):
            self.assertEqual(classify_group_ad(text), "ad", text)
        for text in (
            "سلام دوستان",
            "کسی قیمت موبایل را می‌داند؟",
            "چطور آگهی گوشی فروشی ۳۰۰۰ افغانی را در ربات ثبت کنم؟",
            "این موبایل فروشی نیست، قیمتش ۳۰۰۰ افغانی است",
        ):
            self.assertNotEqual(classify_group_ad(text), "ad", text)
        self.assertEqual(classify_group_ad("", True), "normal")
        self.assertEqual(classify_group_ad("سامسونگ ۳۰۰۰ افغانی"), "review")

    def test_schema5_migration_keeps_ads_and_custom_rules(self):
        m = Marketplace(self.db)
        m.ensure_user(100)
        ad = m.create_draft(100, 1, "sale")
        with self.db.transaction() as c:
            c.execute(
                "UPDATE settings SET value='قانون اختصاصی مدیر' WHERE key='rules'"
            )
            c.execute("PRAGMA user_version=5")
        self.db.initialize()
        self.db.initialize()
        self.assertEqual(m.get_ad(ad)["status"], "draft")
        self.assertEqual(m.get_setting("rules"), "قانون اختصاصی مدیر")
        with self.db.connect() as c:
            self.assertEqual(
                c.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "data.sqlite3")
        self.db.initialize()
        self.p = GroupPolicy(self.db)
        config = Config.from_env(
            {
                "TELEGRAM_BOT_TOKEN": "12345:test",
                "ADMIN_IDS": "41,42",
                "GROUP_ID": "-1001",
                "MINI_APP_URL": "https://bazaar.example",
            }
        )
        self.bot = SimpleNamespace(
            id=999,
            get_chat_member=AsyncMock(side_effect=self.member),
            get_me=AsyncMock(
                return_value=SimpleNamespace(
                    username="ActualBot", has_main_web_app=True
                )
            ),
            delete_message=AsyncMock(),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=1000)),
            edit_message_text=AsyncMock(),
            create_chat_invite_link=AsyncMock(
                return_value=SimpleNamespace(invite_link="https://t.me/+private-link")
            ),
        )
        self.context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "config": config,
                    "group_policy": self.p,
                    "database": self.db,
                    "marketplace": Marketplace(self.db),
                }
            ),
            bot=self.bot,
            job_queue=None,
        )
        self.user = SimpleNamespace(
            id=100,
            is_bot=False,
            first_name="Member",
            username="member",
            full_name="Member",
        )
        self.message = SimpleNamespace(
            message_id=50,
            text="سلام دوستان",
            caption=None,
            photo=[],
            sender_chat=None,
            reply_text=AsyncMock(),
        )
        self.update = SimpleNamespace(
            effective_user=self.user,
            effective_chat=SimpleNamespace(id=-1001, type="supergroup"),
            effective_message=self.message,
            callback_query=None,
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def member(self, chat, uid):
        if uid in {999, 300}:
            return SimpleNamespace(
                status=ChatMemberStatus.ADMINISTRATOR,
                can_delete_messages=True,
                can_invite_users=True,
            )
        return SimpleNamespace(status=ChatMemberStatus.MEMBER)

    def enable(self, field):
        self.p.set_enabled(-1001, field, True, 41)

    def qualify(self):
        self.p.save_link(-1001, 100, "https://t.me/+100")
        for uid in (201, 202, 203):
            self.p.register_join(-1001, uid, "https://t.me/+100")

    async def test_gate_deletes_before_other_handlers_and_sends_one_notice(self):
        self.enable("invite_required")
        with self.assertRaises(ApplicationHandlerStop):
            await enforce_group_message(self.update, self.context)
        self.bot.delete_message.assert_awaited_once_with(-1001, 50)
        self.assertIn("باقی‌مانده: 3", self.bot.send_message.await_args.args[1])
        self.message.message_id = 51
        with self.assertRaises(ApplicationHandlerStop):
            await enforce_group_message(self.update, self.context)
        self.assertEqual(self.bot.delete_message.await_count, 2)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_qualified_member_can_chat_but_direct_ad_is_deleted(self):
        self.enable("invite_required")
        self.enable("ad_delete")
        self.qualify()
        await enforce_group_message(self.update, self.context)
        self.bot.delete_message.assert_not_awaited()
        self.message.text = "گوشی سامسونگ فروشی ۳۰۰۰ افغانی در کابل"
        with self.assertRaises(ApplicationHandlerStop):
            await enforce_group_message(self.update, self.context)
        self.assertIn("مینی‌اپ", self.bot.send_message.await_args.args[1])

    async def test_media_without_caption_is_gated_but_not_guessed_as_ad(self):
        self.enable("ad_delete")
        self.message.text = None
        self.message.photo = [object()]
        await enforce_group_message(self.update, self.context)
        self.bot.delete_message.assert_not_awaited()
        self.enable("invite_required")
        with self.assertRaises(ApplicationHandlerStop):
            await enforce_group_message(self.update, self.context)

    async def test_admin_bot_private_and_other_group_are_exempt(self):
        self.enable("invite_required")
        for uid in (41, 300, 999):
            self.user.id = uid
            await enforce_group_message(self.update, self.context)
        self.user.id = 100
        self.user.is_bot = True
        await enforce_group_message(self.update, self.context)
        self.user.is_bot = False
        self.update.effective_chat.type = "private"
        await enforce_group_message(self.update, self.context)
        self.update.effective_chat.type = "supergroup"
        self.update.effective_chat.id = -1002
        await enforce_group_message(self.update, self.context)
        self.bot.delete_message.assert_not_awaited()

    async def test_unknown_admin_status_and_failed_delete_never_claim_success(self):
        self.enable("invite_required")
        self.bot.get_chat_member.side_effect = Forbidden("denied")
        await enforce_group_message(self.update, self.context)
        self.bot.delete_message.assert_not_awaited()
        self.bot.get_chat_member.side_effect = self.member
        self.bot.delete_message.side_effect = BadRequest("no permission")
        await enforce_group_message(self.update, self.context)
        self.bot.send_message.assert_not_awaited()
        self.assertFalse(self.p.deleted(-1001, 50))

    async def test_anonymous_admin_exempt_but_external_sender_cannot_bypass_gate(self):
        self.enable("invite_required")
        self.message.sender_chat = SimpleNamespace(id=-1001, title="گروه")
        await enforce_group_message(self.update, self.context)
        self.bot.delete_message.assert_not_awaited()
        self.message.sender_chat = SimpleNamespace(id=-2001, title="کانال")
        with self.assertRaises(ApplicationHandlerStop):
            await enforce_group_message(self.update, self.context)
        self.assertIn("حساب شخصی", self.bot.send_message.await_args.args[1])

    async def test_invite_link_is_private_personal_reused_and_requires_membership(self):
        self.update.effective_chat.type = "private"
        await invite_status(self.update, self.context)
        await invite_status(self.update, self.context)
        self.bot.create_chat_invite_link.assert_awaited_once()
        self.assertEqual(self.p.link(-1001, 100), "https://t.me/+private-link")
        self.assertIn("دعوت معتبر: 0", self.bot.edit_message_text.await_args.kwargs["text"])
        self.user.id = 200
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(
            status=ChatMemberStatus.LEFT
        )
        await invite_status(self.update, self.context)
        self.assertIn("ابتدا عضو گروه", self.bot.send_message.await_args.kwargs["text"])
        self.assertIsNone(self.p.link(-1001, 200))

    async def test_membership_counts_actual_join_only_and_excludes_bots(self):
        self.p.save_link(-1001, 100, "https://t.me/+one")
        event = SimpleNamespace(
            chat=SimpleNamespace(id=-1001),
            date=datetime.now(UTC),
            old_chat_member=SimpleNamespace(status=ChatMemberStatus.LEFT),
            new_chat_member=SimpleNamespace(
                status=ChatMemberStatus.MEMBER,
                user=SimpleNamespace(id=201, is_bot=True),
            ),
            invite_link=SimpleNamespace(invite_link="https://t.me/+one"),
            via_join_request=False,
        )
        await membership_update(SimpleNamespace(chat_member=event), self.context)
        self.assertEqual(self.p.count(-1001, 100), 0)
        event.new_chat_member.user.is_bot = False
        await membership_update(SimpleNamespace(chat_member=event), self.context)
        await membership_update(SimpleNamespace(chat_member=event), self.context)
        self.assertEqual(self.p.count(-1001, 100), 1)

    async def test_only_bot_owner_can_enable_and_permissions_are_checked(self):
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=100),
            data="gate:invite_required:on",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        await admin_gate_callback(SimpleNamespace(callback_query=query), self.context)
        self.assertFalse(self.p.settings(-1001)["invite_required"])
        query.from_user.id = 41
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(
            status=ChatMemberStatus.MEMBER
        )
        await admin_gate_callback(SimpleNamespace(callback_query=query), self.context)
        self.assertFalse(self.p.settings(-1001)["invite_required"])
        self.bot.get_chat_member.return_value = SimpleNamespace(
            status=ChatMemberStatus.ADMINISTRATOR,
            can_delete_messages=True,
            can_invite_users=True,
        )
        await admin_gate_callback(SimpleNamespace(callback_query=query), self.context)
        self.assertTrue(self.p.settings(-1001)["invite_required"])

    async def test_refresh_unchanged_panel_is_a_noop_but_other_errors_propagate(self):
        self.enable("invite_required")
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=41),
            data="gate:panel",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified")),
        )
        for _ in range(3):
            await admin_gate_callback(SimpleNamespace(callback_query=query), self.context)
        self.assertEqual(query.answer.await_count, 3)
        self.assertTrue(self.p.settings(-1001)["invite_required"])
        self.assertFalse(self.p.settings(-1001)["ad_delete"])
        query.edit_message_text.side_effect = BadRequest("Message can't be edited")
        with self.assertRaises(BadRequest):
            await admin_gate_callback(SimpleNamespace(callback_query=query), self.context)

    async def test_menu_support_rules_and_charkhak_keep_other_entries(self):
        from handlers.user import _main_menu, help_keyboard, effective_rules

        self.update.effective_chat.type = "private"
        text, keyboard = _main_menu(self.context, self.update)
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertNotIn("📝 ثبت آگهی جدید", labels)
        for label in (
            "🔎 جست‌وجوی آگهی‌ها",
            "📂 دسته‌بندی آگهی‌ها",
            "📋 قوانین بازارچه",
            "🎡 بازی چرخک",
        ):
            self.assertIn(label, labels)
        self.assertEqual(
            keyboard.inline_keyboard[-1][0].url, "https://t.me/CHARKHAK1_BOT"
        )
        self.assertEqual(
            help_keyboard().inline_keyboard[0][0].url, "https://t.me/Khalil_Noori"
        )
        self.assertIn("تضمین", effective_rules(self.context))

    async def test_ambiguous_ad_goes_to_review_without_deletion(self):
        self.enable("ad_delete")
        self.message.text = "سامسونگ ۳۰۰۰ افغانی"
        await enforce_group_message(self.update, self.context)
        self.bot.delete_message.assert_not_awaited()
        self.assertEqual(
            len(
                self.context.application.bot_data["marketplace"].pending_group_reviews()
            ),
            1,
        )
