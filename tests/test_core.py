from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Config, ConfigurationError
from database import _SCHEMA_V1, DEFAULT_CATEGORIES, Database
from services.backup import (
    create_backup,
    prune_backups,
    restore_backup,
    validate_backup,
)
from services.marketplace import Marketplace, MarketplaceError, validate_ad
from services.moderation import Moderation

VALID_ENV = {"TELEGRAM_BOT_TOKEN": "123456:placeholder", "ADMIN_IDS": "41,42"}


class ConfigTests(unittest.TestCase):
    def test_valid_configuration_and_defaults(self):
        config = Config.from_env(VALID_ENV)
        self.assertEqual(config.admin_ids, frozenset({41, 42}))
        self.assertEqual(config.moderation_review_mode, "required")
        self.assertEqual(config.max_active_ads_per_user, 5)
        self.assertEqual(config.automatic_backup_interval_hours, 24)

    def test_missing_token_or_admin_is_rejected(self):
        for env in ({"ADMIN_IDS": "41"}, {"TELEGRAM_BOT_TOKEN": "123:token"}):
            with self.subTest(env=env), self.assertRaises(ConfigurationError):
                Config.from_env(env)

    def test_invalid_values_are_rejected(self):
        for key, value in (("ADMIN_IDS", "x"), ("DEFAULT_AD_EXPIRY_DAYS", "0"), ("AUTOMATIC_BACKUP_INTERVAL_HOURS", "-1"), ("MODERATION_REVIEW_MODE", "maybe"), ("LOG_LEVEL", "VERBOSE")):
            with self.subTest(key=key), self.assertRaises(ConfigurationError):
                Config.from_env({**VALID_ENV, key: value})

    def test_railway_config_selects_railpack_and_bot_entrypoint(self):
        root = Path(__file__).resolve().parents[1]
        with (root / "railway.json").open(encoding="utf-8") as railway_file:
            railway = json.load(railway_file)
        self.assertEqual(railway["build"]["builder"], "RAILPACK")
        self.assertEqual(railway["deploy"]["startCommand"], "python bot.py")
        self.assertEqual((root / ".python-version").read_text(encoding="utf-8").strip(), "3.12")


class ProjectTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "market.sqlite3")
        self.database.initialize()
        self.market = Marketplace(self.database, max_active_ads=5, expiry_days=2)
        self.market.ensure_user(100, "seller", "Seller")
        self.market.ensure_user(200, "buyer", "Buyer")
        self.category_id = self.market.categories()[0]["category_id"]

    def tearDown(self):
        self.temp.cleanup()

    def make_draft(self, owner_id=100, title="گوشی خوب", description="حافظه ۱۲۸ گیگ", city="کابل"):
        ad_id = self.market.create_draft(owner_id, self.category_id, "sale")
        values = {"title": title, "description": description, "price_minor": 12000, "city": city, "contact": "0700000000"}
        for field, value in values.items():
            self.market.update_draft_field(ad_id, owner_id, field, value)
        return ad_id

    def publish(self, owner_id=100, title="گوشی خوب", description="حافظه ۱۲۸ گیگ", city="کابل"):
        ad_id = self.make_draft(owner_id, title, description, city)
        self.assertEqual(self.market.submit(ad_id, owner_id), "pending")
        self.market.transition(ad_id, "approved", admin_id=41)
        self.assertTrue(self.market.claim_publication(ad_id, 41, -1001))
        self.market.finish_publication(ad_id, 701 + ad_id)
        return ad_id


class DatabaseTests(ProjectTestCase):
    def test_initialization_seed_and_integrity_are_repeatable(self):
        self.database.initialize()
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT count(*) FROM categories").fetchone()[0], len(DEFAULT_CATEGORIES))
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_v1_migration_preserves_existing_users(self):
        old_path = self.root / "v1.sqlite3"
        connection = sqlite3.connect(old_path)
        connection.executescript(_SCHEMA_V1)
        connection.executemany("INSERT INTO categories(name,sort_order) VALUES(?,?)", [(name, i) for i, name in enumerate(DEFAULT_CATEGORIES)])
        connection.execute("INSERT INTO users(user_id,display_name) VALUES(77,'پیشین')")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()
        migrated = Database(old_path)
        migrated.initialize()
        with migrated.connect() as conn:
            self.assertEqual(conn.execute("SELECT display_name FROM users WHERE user_id=77").fetchone()[0], "پیشین")
            self.assertIsNotNone(conn.execute("SELECT expiry_notified_at FROM ads LIMIT 1").description)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_v2_migration_adds_sessions_jobs_and_album_message_tables(self):
        with self.database.transaction() as conn:
            conn.execute("DROP TABLE ad_publication_messages")
            conn.execute("DROP TABLE scheduled_state")
            conn.execute("DROP TABLE search_sessions")
            conn.execute("PRAGMA user_version=2")
        self.database.initialize()
        with self.database.connect() as conn:
            table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"search_sessions", "scheduled_state", "ad_publication_messages"}.issubset(table_names))
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_scheduled_job_claim_is_single_owner_and_retryable(self):
        barrier = threading.Barrier(2)
        results = []

        def claim():
            barrier.wait()
            results.append(self.market.claim_scheduled_job("test-job", 24))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(results), [False, True])
        self.market.finish_scheduled_job("test-job", False)
        self.assertTrue(self.market.claim_scheduled_job("test-job", 24))
        self.market.finish_scheduled_job("test-job", True)
        self.assertFalse(self.market.claim_scheduled_job("test-job", 24))

    def test_temporary_cleanup_only_removes_expired_session_state(self):
        old_session = self.market.create_search_session(100)
        current_session = self.market.create_search_session(200)
        with self.database.transaction() as conn:
            conn.execute("UPDATE search_sessions SET created_at='2000-01-01T00:00:00Z' WHERE session_id=?", (old_session,))
            conn.execute("INSERT INTO processed_updates(update_id,processed_at) VALUES(1,'2000-01-01T00:00:00Z')")
        self.assertEqual(self.market.cleanup_temporary_state(30), 1)
        self.assertIsNone(self.market.get_search_session(old_session, 100))
        self.assertIsNotNone(self.market.get_search_session(current_session, 200))
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM processed_updates").fetchone()[0], 0)


class AdvertisementTests(ProjectTestCase):
    def test_validation_preserves_price_and_product_text(self):
        data = {"title": " گوشی ۱۲۸ گیگ ", "description": "قیمت ۱۲٬۰۰۰ افغانی", "city": "کابل", "contact": "07000", "ad_type": "sale", "price_minor": 12000}
        cleaned = validate_ad(data)
        self.assertEqual(cleaned["title"], "گوشی ۱۲۸ گیگ")
        self.assertEqual(cleaned["price_minor"], 12000)
        self.assertIn("۱۲٬۰۰۰", cleaned["description"])

    def test_markup_characters_are_not_removed_from_ad_text(self):
        data = {"title": "نمونه <دستگاه>", "description": "ویژه & اصلی", "city": "کابل", "contact": "07000", "ad_type": "sale", "price_minor": 100}
        cleaned = validate_ad(data)
        self.assertEqual(cleaned["title"], "نمونه <دستگاه>")
        self.assertEqual(cleaned["description"], "ویژه & اصلی")

    def test_validation_rejects_missing_and_implausible_values(self):
        with self.assertRaises(MarketplaceError):
            validate_ad({"title": "", "description": "d", "city": "c", "contact": "x", "ad_type": "sale", "price_minor": 1})
        with self.assertRaises(MarketplaceError):
            validate_ad({"title": "t", "description": "d", "city": "c", "contact": "x", "ad_type": "sale", "price_minor": -1})

    def test_owner_check_and_submission(self):
        ad_id = self.make_draft()
        with self.assertRaises(MarketplaceError):
            self.market.update_draft_field(ad_id, 200, "title", "تصرف غیرمجاز")
        self.assertEqual(self.market.submit(ad_id, 100), "pending")

    def test_reputation_is_granted_once_only_after_approval(self):
        ad_id = self.make_draft()
        self.market.submit(ad_id, 100, review_required=False)
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("SELECT reputation FROM users WHERE user_id=100").fetchone()[0], 0)
        self.market.transition(ad_id, "publishing")
        self.market.transition(ad_id, "publication_failed")
        with self.assertRaises(MarketplaceError):
            self.market.transition(ad_id, "approved")
        second = self.make_draft(title="آگهی دوم")
        self.market.submit(second, 100)
        self.market.transition(second, "approved", admin_id=41)
        self.market.transition(second, "archived", admin_id=41)
        self.market.reopen(second, 100)
        self.market.submit(second, 100)
        self.market.transition(second, "approved", admin_id=41)
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("SELECT reputation FROM users WHERE user_id=100").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM reputation_events WHERE ad_id=?", (second,)).fetchone()[0], 1)

    def test_weekly_rankings_include_approved_ads_and_are_limited(self):
        first = self.make_draft(title="رتبه نخست")
        self.market.submit(first, 100)
        self.market.transition(first, "approved", admin_id=41)
        rows = self.market.seller_rankings()
        self.assertEqual(rows[0]["user_id"], 100)
        self.assertEqual(rows[0]["score"], 1)
        self.assertEqual(rows[0]["approved_ads"], 1)

    def test_duplicate_submission_is_rejected(self):
        self.market.submit(self.make_draft(), 100)
        duplicate = self.make_draft()
        with self.assertRaises(MarketplaceError):
            self.market.submit(duplicate, 100)

    def test_invalid_status_transition_is_rejected(self):
        ad_id = self.make_draft()
        with self.assertRaises(MarketplaceError):
            self.market.transition(ad_id, "published", admin_id=41)

    def test_publication_claim_cannot_be_repeated(self):
        ad_id = self.publish()
        self.assertFalse(self.market.claim_publication(ad_id, 41, -1001))
        with self.assertRaises(MarketplaceError):
            self.market.finish_publication(ad_id, 900)

    def test_interrupted_publication_is_not_automatically_retried(self):
        ad_id = self.make_draft()
        self.market.submit(ad_id, 100)
        self.market.transition(ad_id, "approved", admin_id=41)
        self.assertTrue(self.market.claim_publication(ad_id, 41, -1001))
        self.database.initialize()
        self.assertFalse(self.market.claim_publication(ad_id, 41, -1001))
        self.assertEqual(self.market.get_ad(ad_id)["status"], "publishing")

    def test_explicit_retry_only_reopens_known_failed_publications(self):
        ad_id = self.make_draft()
        self.market.submit(ad_id, 100)
        self.market.transition(ad_id, "approved", admin_id=41)
        self.assertTrue(self.market.claim_publication(ad_id, 41, -1001))
        self.market.fail_publication(ad_id, 41)
        self.assertFalse(self.market.claim_publication(ad_id, 41, -1001))
        self.assertTrue(self.market.claim_publication(ad_id, 41, -1001, retry_failed=True))

    def test_admin_can_confirm_partial_publication_without_resending(self):
        ad_id = self.make_draft()
        self.market.submit(ad_id, 100)
        self.market.transition(ad_id, "approved", admin_id=41)
        self.market.claim_publication(ad_id, 41, -1001)
        self.market.record_publication_message(ad_id, 920)
        self.market.fail_publication(ad_id, 41)
        self.market.confirm_partial_publication(ad_id, 41)
        self.assertEqual(self.market.get_ad(ad_id)["status"], "published")
        self.assertEqual(self.market.get_ad(ad_id)["publication_message_id"], 920)

    def test_search_filters_and_pagination(self):
        ids = [self.publish(title=f"گوشی {n}", city="کابل" if n < 2 else "هرات") for n in range(3)]
        self.assertEqual(len(self.market.search(keyword="گوشی", page_size=2)), 2)
        self.assertEqual(len(self.market.search(city="هرات")), 1)
        self.assertEqual(len(self.market.search(min_price=13000)), 0)
        self.assertEqual(len(self.market.search(page=1, page_size=2)), 1)
        self.assertNotEqual(ids[0], ids[1])

    def test_persisted_search_sessions_apply_filters_and_are_user_scoped(self):
        first = self.publish(title="گوشی ۱۲۸", city="کابل")
        self.publish(title="لپ‌تاپ", city="هرات")
        session_id = self.market.create_search_session(100, "گوشی")
        self.market.update_search_session(session_id, 100, "category_id", self.category_id)
        self.market.update_search_session(session_id, 100, "city", "کابل")
        self.market.update_search_session(session_id, 100, "ad_type", "sale")
        self.market.update_search_session(session_id, 100, "min_price", 10000)
        self.market.update_search_session(session_id, 100, "max_price", 15000)
        self.assertEqual(self.market.search_count(keyword="گوشی", category_id=self.category_id, city="کابل", ad_type="sale", min_price=10000, max_price=15000), 1)
        self.assertEqual(self.market.search(keyword="گوشی", category_id=self.category_id, city="کابل", ad_type="sale", min_price=10000, max_price=15000)[0]["ad_id"], first)
        self.assertIsNone(self.market.get_search_session(session_id, 200))
        self.assertEqual(self.market.get_search_session(session_id, 100)["keyword"], "گوشی")

    def test_persian_price_filter_input_and_invalid_range(self):
        from handlers.user import search_filter_text

        session_id = self.market.create_search_session(100)
        self.market.update_search_session(session_id, 100, "pending_field", "price")
        message = SimpleNamespace(text="۱۰۰۰-۵۰۰۰", reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=100), effective_message=message, callback_query=None)
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"marketplace": self.market}))
        asyncio.run(search_filter_text(update, context))
        session = self.market.get_search_session(session_id, 100)
        self.assertEqual((session["min_price"], session["max_price"], session["pending_field"]), (1000, 5000, None))
        self.market.update_search_session(session_id, 100, "pending_field", "price")
        message.text = "۹۰۰۰-۱۰۰۰"
        asyncio.run(search_filter_text(update, context))
        self.assertEqual(self.market.get_search_session(session_id, 100)["pending_field"], "price")

    def test_report_duplicate_and_rate_limit(self):
        first, second, third, fourth = (self.publish(title=f"آگهی {n}") for n in range(4))
        self.market.add_report(first, 200, "fraud")
        with self.assertRaises(MarketplaceError):
            self.market.add_report(first, 200, "fraud")
        self.market.add_report(second, 200, "spam")
        self.market.add_report(third, 200, "other")
        with self.assertRaises(MarketplaceError):
            self.market.add_report(fourth, 200, "other")

    def test_expiration_marks_only_published_ads(self):
        ad_id = self.publish()
        with self.database.transaction() as conn:
            conn.execute("UPDATE ads SET expires_at=? WHERE ad_id=?", ((datetime.now(UTC) - timedelta(days=1)).isoformat(), ad_id))
        self.assertEqual(self.market.expire_ads(), 1)
        self.assertEqual(self.market.get_ad(ad_id)["status"], "expired")

    def test_expiry_notification_is_claimed_at_most_once(self):
        ad_id = self.publish()
        with self.database.transaction() as conn:
            conn.execute("UPDATE ads SET expires_at=? WHERE ad_id=?", ((datetime.now(UTC) + timedelta(hours=12)).isoformat(), ad_id))
        self.assertEqual(len(self.market.due_expiry_notifications()), 1)
        self.assertTrue(self.market.mark_expiry_notified(ad_id))
        self.assertFalse(self.market.mark_expiry_notified(ad_id))
        self.assertEqual(self.market.due_expiry_notifications(), [])

    def test_owner_can_mark_sold_and_cannot_change_other_users_ad(self):
        ad_id = self.publish()
        with self.assertRaises(MarketplaceError):
            self.market.owner_action(ad_id, 200, "sold")
        self.market.owner_action(ad_id, 100, "sold")
        self.assertEqual(self.market.get_ad(ad_id)["status"], "sold")

    def test_rejected_ad_can_be_reopened_and_edited_by_owner(self):
        ad_id = self.make_draft()
        self.market.submit(ad_id, 100)
        self.market.transition(ad_id, "rejected", admin_id=41)
        with self.assertRaises(MarketplaceError):
            self.market.reopen(ad_id, 200)
        self.market.reopen(ad_id, 100)
        self.market.update_draft_field(ad_id, 100, "title", "عنوان اصلاح‌شده")
        self.assertEqual(self.market.owned_ad(ad_id, 100)["title"], "عنوان اصلاح‌شده")

    def test_category_rename_disable_and_reorder_preserve_ads(self):
        category_ids = [row["category_id"] for row in self.market.categories()]
        ad_id = self.make_draft()
        self.market.rename_category(category_ids[0], "تکنالوژی تازه", 41)
        self.market.reorder_category(category_ids[0], len(category_ids), 41)
        self.assertEqual(self.market.categories()[-1]["name"], "تکنالوژی تازه")
        with self.database.transaction() as conn:
            conn.execute("UPDATE categories SET enabled=0 WHERE category_id=?", (category_ids[0],))
            self.assertEqual(conn.execute("SELECT category_id FROM ads WHERE ad_id=?", (ad_id,)).fetchone()[0], category_ids[0])
        with self.assertRaises(MarketplaceError):
            self.market.rename_category(category_ids[0], "", 41)


class ModerationTests(ProjectTestCase):
    def test_frequency_repeat_blocked_phrase_links_and_exemptions(self):
        moderation = Moderation(self.database, max_messages=3, window_seconds=30, repeat_limit=2)
        self.assertIsNone(moderation.check_message(1, "سوال عادی خرید؟"))
        self.assertIsNone(moderation.check_message(1, "سوال عادی خرید؟"))
        self.assertEqual(moderation.check_message(1, "سوال عادی خرید؟"), "repeated")
        self.assertEqual(moderation.check_message(2, "عبارت بسته", blocked_phrases=("عبارت بسته",)), "blocked_phrase")
        self.assertEqual(moderation.check_message(3, "https://example.test", links_allowed=False), "link")
        self.assertIsNone(moderation.check_message(4, "سلام", admin=True))
        self.assertIsNone(moderation.check_message(5, "سلام", trusted=True))

    def test_warning_history_escalates_by_count(self):
        moderation = Moderation(self.database)
        self.assertEqual(moderation.warn(300, 41, "دلیل نخست"), 1)
        self.assertEqual(moderation.warn(300, 41, "دلیل دوم"), 2)
        self.assertEqual(moderation.warning_count(300), 2)


class AuthorizationAndBackupTests(ProjectTestCase):
    def test_admin_allowlist_and_callback_denial(self):
        from handlers.admin import _dashboard_callback, _is_admin

        context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": Config.from_env(VALID_ENV)}))
        self.assertTrue(_is_admin(context, 41))
        self.assertFalse(_is_admin(context, 99))
        query = SimpleNamespace(from_user=SimpleNamespace(id=99), data="admin:stats", answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        asyncio.run(_dashboard_callback(update, context))
        query.answer.assert_awaited_once()
        query.edit_message_text.assert_not_awaited()

    def test_each_review_callback_checks_admin_id(self):
        from handlers.admin import _admin_action

        ad_id = self.make_draft()
        self.market.submit(ad_id, 100)
        query = SimpleNamespace(from_user=SimpleNamespace(id=99), data=f"ad:approve:{ad_id}", answer=AsyncMock(), edit_message_text=AsyncMock())
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": Config.from_env(VALID_ENV), "marketplace": self.market}))
        asyncio.run(_admin_action(SimpleNamespace(callback_query=query), context))
        query.answer.assert_awaited_once()
        query.edit_message_text.assert_not_awaited()
        self.assertEqual(self.market.get_ad(ad_id)["status"], "pending")

    def test_new_admin_category_and_ranking_callbacks_require_allowlist(self):
        from handlers.admin import _category_toggle, _settings

        category_id = self.market.categories()[0]["category_id"]
        config = Config.from_env(VALID_ENV)
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": config, "database": self.database, "marketplace": self.market}))
        denied_query = SimpleNamespace(from_user=SimpleNamespace(id=99), data=f"category:{category_id}", answer=AsyncMock(), edit_message_text=AsyncMock())
        asyncio.run(_category_toggle(SimpleNamespace(callback_query=denied_query), context))
        self.assertEqual(self.market.categories()[0]["enabled"], 1)
        allowed_query = SimpleNamespace(from_user=SimpleNamespace(id=41), data="setting:public_rankings", answer=AsyncMock(), edit_message_text=AsyncMock())
        asyncio.run(_settings(SimpleNamespace(callback_query=allowed_query), context))
        self.assertEqual(self.market.get_setting("public_rankings"), "0")

    def test_search_callback_rejects_another_users_session(self):
        from handlers.user import search_callback

        session_id = self.market.create_search_session(100)
        query = SimpleNamespace(from_user=SimpleNamespace(id=200), data=f"sr:{session_id}:city", answer=AsyncMock(), edit_message_text=AsyncMock(), message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"marketplace": self.market}))
        asyncio.run(search_callback(SimpleNamespace(callback_query=query, effective_user=query.from_user), context))
        query.edit_message_text.assert_awaited_once()
        self.assertIsNone(self.market.pending_search_input(200))

    def test_public_rankings_can_be_disabled(self):
        from handlers.user import rankings

        self.market.set_setting("public_rankings", "0", 41)
        reply = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"marketplace": self.market}))
        asyncio.run(rankings(update, context))
        reply.assert_awaited_once()
        self.assertIn("خاموش", reply.await_args.args[0])

    def test_report_review_callback_parses_and_records_decision(self):
        from handlers.admin import _admin_action

        ad_id = self.publish()
        report_id = self.market.add_report(ad_id, 200, "spam")
        query = SimpleNamespace(from_user=SimpleNamespace(id=41), data=f"report-review:{report_id}", answer=AsyncMock(), edit_message_text=AsyncMock())
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": Config.from_env(VALID_ENV), "marketplace": self.market}))
        asyncio.run(_admin_action(SimpleNamespace(callback_query=query), context))
        with self.database.connect() as conn:
            status = conn.execute("SELECT status FROM reports WHERE report_id=?", (report_id,)).fetchone()[0]
        self.assertEqual(status, "reviewed")
        query.edit_message_text.assert_awaited_once()

    def test_bot_builds_without_real_token_or_network(self):
        from bot import build_application

        config = Config.from_env({**VALID_ENV, "DATABASE_PATH": str(self.root / "app.sqlite3")})
        application = build_application(config)
        self.assertIn("marketplace", application.bot_data)
        self.assertTrue(application.handlers)

    def test_telegram_send_failure_is_recorded_and_not_leaked(self):
        from telegram.error import TelegramError

        from handlers.admin import _publish_ad

        ad_id = self.make_draft()
        self.market.submit(ad_id, 100)
        self.market.transition(ad_id, "approved", admin_id=41)
        config = Config.from_env({**VALID_ENV, "AD_PUBLICATION_CHAT_ID": "-1001234567890"})
        fake_bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("private network detail")))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": config, "marketplace": self.market}), bot=fake_bot)
        asyncio.run(_publish_ad(context, ad_id, 41))
        self.assertEqual(self.market.get_ad(ad_id)["status"], "publication_failed")
        fake_bot.send_message.assert_awaited_once()

    def test_photo_message_prevents_duplicate_if_details_send_fails(self):
        from telegram.error import TelegramError

        from handlers.admin import _publish_ad

        ad_id = self.make_draft()
        self.market.add_photo(ad_id, 100, "telegram-file-id")
        self.market.submit(ad_id, 100)
        self.market.transition(ad_id, "approved", admin_id=41)
        config = Config.from_env({**VALID_ENV, "AD_PUBLICATION_CHAT_ID": "-1001234567890"})
        sent_photo = SimpleNamespace(message_id=808)
        fake_bot = SimpleNamespace(send_photo=AsyncMock(return_value=sent_photo), send_message=AsyncMock(side_effect=TelegramError("details failed")))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": config, "marketplace": self.market}), bot=fake_bot)
        asyncio.run(_publish_ad(context, ad_id, 41))
        ad = self.market.get_ad(ad_id)
        self.assertEqual(ad["status"], "publication_failed")
        self.assertEqual(ad["publication_message_id"], 808)
        self.assertFalse(self.market.claim_publication(ad_id, 41, -1001234567890, retry_failed=True))

    def test_photo_album_publishes_and_tracks_every_message(self):
        from handlers.admin import _publish_ad

        ad_id = self.make_draft()
        for file_id in ("photo-1", "photo-2", "photo-3"):
            self.market.add_photo(ad_id, 100, file_id)
        self.market.submit(ad_id, 100)
        self.market.transition(ad_id, "approved", admin_id=41)
        config = Config.from_env({**VALID_ENV, "AD_PUBLICATION_CHAT_ID": "-1001234567890"})
        album_messages = [SimpleNamespace(message_id=value) for value in (801, 802, 803)]
        sent_messages = [SimpleNamespace(message_id=value) for value in (804, 805, 806)]
        fake_bot = SimpleNamespace(send_media_group=AsyncMock(return_value=album_messages), send_message=AsyncMock(side_effect=sent_messages))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": config, "marketplace": self.market}), bot=fake_bot)
        asyncio.run(_publish_ad(context, ad_id, 41))
        self.assertEqual(self.market.get_ad(ad_id)["status"], "published")
        rows = self.market.publication_messages(ad_id)
        self.assertEqual([row["message_id"] for row in rows], [801, 802, 803, 804])
        fake_bot.send_media_group.assert_awaited_once()
        self.assertEqual(len(fake_bot.send_media_group.await_args.args[1]), 3)

    def test_failed_album_followup_cannot_retry_album_into_duplicates(self):
        from telegram.error import TelegramError

        from handlers.admin import _publish_ad

        ad_id = self.make_draft()
        self.market.add_photo(ad_id, 100, "photo-1")
        self.market.add_photo(ad_id, 100, "photo-2")
        self.market.submit(ad_id, 100)
        self.market.transition(ad_id, "approved", admin_id=41)
        config = Config.from_env({**VALID_ENV, "AD_PUBLICATION_CHAT_ID": "-1001234567890"})
        album_messages = [SimpleNamespace(message_id=value) for value in (901, 902)]
        fake_bot = SimpleNamespace(send_media_group=AsyncMock(return_value=album_messages), send_message=AsyncMock(side_effect=TelegramError("details unavailable")))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": config, "marketplace": self.market}), bot=fake_bot)
        asyncio.run(_publish_ad(context, ad_id, 41))
        self.assertEqual(self.market.get_ad(ad_id)["status"], "publication_failed")
        self.assertEqual([row["message_id"] for row in self.market.publication_messages(ad_id)], [901, 902])
        self.assertFalse(self.market.claim_publication(ad_id, 41, -1001234567890, retry_failed=True))
        self.market.confirm_partial_publication(ad_id, 41, [901, 902])
        self.assertEqual(self.market.get_ad(ad_id)["status"], "published")
        self.assertEqual([row["message_id"] for row in self.market.publication_messages(ad_id)], [901, 902])

    def test_backup_integrity_and_restore(self):
        ad_id = self.make_draft()
        backup_path = create_backup(self.database, self.root / "backups")
        validate_backup(backup_path)
        restored = Database(self.root / "restored.sqlite3")
        restored.initialize()
        Marketplace(restored).ensure_user(555, display_name="باید پیش از بازیابی پشتیبان شود")
        with self.assertRaises(ValueError):
            restore_backup(restored, backup_path, self.root / "restore-safety")
        pre_restore = restore_backup(restored, backup_path, self.root / "restore-safety", confirmed=True)
        validate_backup(pre_restore)
        with restored.connect() as conn:
            self.assertEqual(conn.execute("SELECT title FROM ads WHERE ad_id=?", (ad_id,)).fetchone()[0], "گوشی خوب")
            self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE user_id=555").fetchone()[0], 0)
        recovery = Database(self.root / "recovery.sqlite3")
        recovery.initialize()
        restore_backup(recovery, pre_restore, self.root / "restore-safety", confirmed=True)
        with recovery.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE user_id=555").fetchone()[0], 1)

    def test_restore_command_requires_admin_and_confirmation(self):
        from handlers.admin import restore_database_command

        backup_directory = self.root / "admin-backups"
        backup_directory.mkdir()
        backup_source = self.root / "source.sqlite3"
        source_db = Database(backup_source)
        source_db.initialize()
        Marketplace(source_db).ensure_user(777, display_name="نسخهٔ پشتیبان")
        source_file = create_backup(source_db, backup_directory)
        config = Config.from_env({**VALID_ENV, "BACKUP_DIRECTORY": str(backup_directory)})
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=99), effective_message=SimpleNamespace(reply_text=reply))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": config, "database": self.database}), args=[source_file.name, "تأیید"])
        asyncio.run(restore_database_command(update, context))
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE user_id=777").fetchone()[0], 0)
        update.effective_user.id = 41
        context.args = [source_file.name, "نه"]
        asyncio.run(restore_database_command(update, context))
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE user_id=777").fetchone()[0], 0)
        context.args = [source_file.name, "تأیید"]
        asyncio.run(restore_database_command(update, context))
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE user_id=777").fetchone()[0], 1)
        safety_copies = [path for path in backup_directory.glob("bazaarche-*.sqlite3") if path != source_file]
        self.assertEqual(len(safety_copies), 1, [path.name for path in backup_directory.iterdir()])
        reply.assert_awaited()

    def test_backup_pruning_only_removes_old_managed_files(self):
        directory = self.root / "retained"
        directory.mkdir()
        old = create_backup(self.database, directory)
        old.touch()
        old_time = datetime.now(UTC).timestamp() - 40 * 86400
        import os
        os.utime(old, (old_time, old_time))
        unrelated = directory / "do-not-delete.sqlite3"
        unrelated.write_text("keep", encoding="utf-8")
        self.assertEqual(prune_backups(directory, 14), 1)
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())

    def test_backup_name_collision_never_overwrites_existing_file(self):
        fixed_time = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
        class FixedDateTime:
            @staticmethod
            def now(_timezone):
                return fixed_time

        directory = self.root / "collisions"
        with patch("services.backup.datetime", FixedDateTime):
            existing = create_backup(self.database, directory)
            original = existing.read_bytes()
            with self.assertRaises(FileExistsError):
                create_backup(self.database, directory)
        self.assertEqual(existing.read_bytes(), original)
        validate_backup(existing)

    def test_invalid_restore_and_prebackup_failure_leave_database_unchanged(self):
        from services.backup import restore_backup

        self.market.ensure_user(666, display_name="دیتای زنده")
        invalid = self.root / "invalid.sqlite3"
        invalid.write_text("not a sqlite database", encoding="utf-8")
        with self.assertRaises(sqlite3.DatabaseError):
            restore_backup(self.database, invalid, self.root / "safe-backups", confirmed=True)
        unsafe_directory = self.root / "backup-link"
        real_directory = self.root / "real-backups"
        real_directory.mkdir()
        unsafe_directory.symlink_to(real_directory, target_is_directory=True)
        valid_backup = create_backup(self.database, real_directory)
        with self.assertRaises(ValueError):
            restore_backup(self.database, valid_backup, unsafe_directory, confirmed=True)
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("SELECT display_name FROM users WHERE user_id=666").fetchone()[0], "دیتای زنده")

    def test_failed_staged_restore_validation_never_replaces_live_file(self):
        import services.backup as backup_service

        self.market.ensure_user(888, display_name="زنده")
        source_db = Database(self.root / "restore-source.sqlite3")
        source_db.initialize()
        Marketplace(source_db).ensure_user(999, display_name="پشتیبان")
        source = create_backup(source_db, self.root / "source-backups")
        original_validation = backup_service.validate_backup

        def reject_staged_file(path):
            if path.name.endswith(".restore"):
                raise ValueError("staged file deliberately rejected")
            return original_validation(path)

        with (
            patch("services.backup.validate_backup", side_effect=reject_staged_file),
            self.assertRaises(ValueError),
        ):
            backup_service.restore_backup(self.database, source, self.root / "safety", confirmed=True)
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("SELECT display_name FROM users WHERE user_id=888").fetchone()[0], "زنده")
            self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE user_id=999").fetchone()[0], 0)
        self.assertEqual(list(self.root.glob(".*.restore")), [])

    def test_scheduled_task_creates_only_one_backup_per_interval(self):
        from bot import _scheduled_tasks

        backup_directory = self.root / "scheduled-backups"
        config = Config.from_env({**VALID_ENV, "DATABASE_PATH": str(self.database.path), "BACKUP_DIRECTORY": str(backup_directory)})
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"config": config, "database": self.database, "marketplace": self.market}),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )
        asyncio.run(_scheduled_tasks(context))
        asyncio.run(_scheduled_tasks(context))
        backups = list(backup_directory.glob("bazaarche-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(self.market.claim_scheduled_job("automatic_backup", 24) is False)


if __name__ == "__main__":
    unittest.main()