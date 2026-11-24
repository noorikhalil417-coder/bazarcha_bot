import asyncio
import base64
import io
import uuid
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from telegram.error import Forbidden, NetworkError, RetryAfter

import test_miniapp as fixtures
import test_marketplace_web as market_fixtures
from database import Database, SCHEMA_VERSION
from services.backup import create_backup, restore_backup
from services.chat_notifications import NotificationWorker, recover_inflight
from services.miniapp import create_web_app


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ApiTests.asyncSetUp(self)
        await self.client.close()
        self.config = replace(self.config, admin_ids=frozenset({41, 42}), chat_reviewer_ids=frozenset({41}))
        self.application.bot_data["config"] = self.config
        self.client = TestClient(TestServer(create_web_app(self.application)))
        await self.client.start_server()
        self.ad = await market_fixtures.MarketplaceWebTests.publish_ad(self, owner=100, with_photo=True)

    async def asyncTearDown(self):
        await fixtures.ApiTests.asyncTearDown(self)

    def headers(self, uid=100):
        return fixtures.ApiTests.headers(self, uid)

    async def put(self, body=None, uid=100):
        return await fixtures.ApiTests.put(self, body, uid)

    async def start(self, uid=200):
        response = await self.client.post('/api/chat/start', json={'ad_id': self.ad}, headers=self.headers(uid))
        self.assertEqual(response.status, 200)
        return (await response.json())['conversation_id']

    async def send(self, cid, uid, text='سلام', image='', key=None):
        return await self.client.post(f'/api/chat/{cid}/messages',
                                      json={'text': text, 'image': image, 'client_id': key or str(uuid.uuid4())},
                                      headers=self.headers(uid))

    async def test_private_messages_images_read_and_search(self):
        cid = await self.start()
        self.assertEqual((await self.client.post('/api/chat/start', json={'ad_id':self.ad}, headers=self.headers(100))).status, 400)
        self.assertEqual((await self.client.get(f'/api/chat/{cid}/messages', headers=self.headers(300))).status, 404)
        data = io.BytesIO()
        Image.new('RGB', (12, 12), 'blue').save(data, 'PNG')
        sent = await self.send(cid, 200, image=base64.b64encode(data.getvalue()).decode())
        self.assertEqual(sent.status, 200)
        mid = (await sent.json())['message_id']
        self.assertEqual((await self.client.get(f'/api/chat/{cid}/image/{mid}', headers=self.headers(300))).status, 404)
        image = await self.client.get(f'/api/chat/{cid}/image/{mid}', headers=self.headers(100))
        self.assertEqual(image.status, 200)
        self.assertEqual(image.content_type, 'image/jpeg')
        self.assertEqual((await self.client.get(f'/api/chat/{cid}/ad-photo', headers=self.headers(300))).status, 404)
        self.assertEqual((await self.client.get(f'/api/chat/{cid}/ad-photo', headers=self.headers(100))).status, 200)
        seller = await (await self.client.get('/api/chat/conversations?q=سامسونگ', headers=self.headers(100))).json()
        self.assertEqual(seller['unread'], 1)
        self.assertEqual(seller['items'][0]['conversation_id'], cid)
        self.assertEqual((await self.client.post(f'/api/chat/{cid}/read', json={'through':mid+1}, headers=self.headers(100))).status, 400)
        self.assertEqual((await self.client.post(f'/api/chat/{cid}/read', json={'through':mid}, headers=self.headers(100))).status, 200)
        buyer = await (await self.client.get(f'/api/chat/{cid}/messages', headers=self.headers(200))).json()
        self.assertTrue(buyer['items'][0]['read'])

    async def test_duplicate_spam_block_and_notification(self):
        cid = await self.start()
        key = str(uuid.uuid4())
        first, retry = await asyncio.gather(self.send(cid, 200, key=key), self.send(cid, 200, key=key))
        self.assertEqual(first.status, 200)
        self.assertEqual(retry.status, 200)
        self.assertEqual((await first.json())['message_id'], (await retry.json())['message_id'])
        self.assertEqual((await self.send(cid, 200)).status, 400)
        self.bot.send_message.reset_mock()
        self.assertEqual((await self.send(cid, 200, text='پیام دوم')).status, 200)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual((await self.client.post(f'/api/chat/{cid}/block', json={'blocked':True}, headers=self.headers(100))).status, 200)
        self.assertEqual((await self.send(cid, 200, text='مسدود')).status, 400)
        self.assertEqual((await self.send(cid, 100, text='مسدود')).status, 400)
        self.assertEqual((await self.client.post(f'/api/chat/{cid}/block', json={'blocked':False}, headers=self.headers(100))).status, 200)
        self.assertEqual((await self.send(cid, 200, text='آزاد')).status, 200)

    async def test_report_only_explicit_reviewers_access_reported_evidence_and_audit(self):
        cid = await self.start()
        data = io.BytesIO()
        Image.new('RGB', (12, 12), 'red').save(data, 'PNG')
        mid = (await (await self.send(cid, 200, '<img onerror=alert(1)>',
                                      base64.b64encode(data.getvalue()).decode())).json())['message_id']
        report = await self.client.post(f'/api/chat/{cid}/report', json={'message_id':mid,'reason':'تخلف'}, headers=self.headers(100))
        self.assertEqual(report.status, 200)
        rid = (await report.json())['report_id']
        path = f'/api/admin/chat-reports/{rid}'
        self.assertEqual((await self.client.get(path, headers=self.headers(200))).status, 403)
        self.assertEqual((await self.client.get(path, headers=self.headers(42))).status, 403)
        self.assertEqual((await self.client.get(path+'/image', headers=self.headers(42))).status, 403)
        self.assertEqual((await self.client.get(f'/api/chat/{cid}/messages', headers=self.headers(41))).status, 404)
        evidence = await self.client.get(path, headers=self.headers(41))
        self.assertEqual((await evidence.json())['body'], '<img onerror=alert(1)>')
        self.assertEqual((await self.client.get(path+'/image', headers=self.headers(41))).status, 200)
        self.assertEqual((await self.client.post(path+'/review', json={}, headers=self.headers(41))).status, 200)
        with self.db.connect() as conn:
            actions = [row[0] for row in conn.execute("SELECT action FROM admin_actions WHERE action LIKE 'chat_report_%'")]
        self.assertEqual(actions, ['chat_report_view', 'chat_report_image_view', 'chat_report_review'])

    async def test_rate_limit_and_invalid_images(self):
        cid = await self.start()
        invalid = await self.send(cid, 200, text='', image=base64.b64encode(b'<script/>').decode())
        self.assertEqual(invalid.status, 400)
        for i in range(20):
            self.assertEqual((await self.send(cid, 200, text=f'پیام {i}')).status, 200)
        self.assertEqual((await self.send(cid, 200, text='بیش از حد')).status, 400)

    async def test_report_pagination_has_no_hidden_older_reports(self):
        cid = await self.start()
        with self.db.transaction() as conn:
            for i in range(51):
                mid = conn.execute(
                    "INSERT INTO chat_messages(conversation_id,sender_id,client_id,body,fingerprint) VALUES(?,?,?,?,?)",
                    (cid, 200, str(uuid.uuid4()), str(i), str(i)),
                ).lastrowid
                conn.execute("INSERT INTO chat_reports(conversation_id,message_id,reporter_id,reason) VALUES(?,?,?,?)",
                             (cid, mid, 100, 'تخلف'))
        first = await (await self.client.get('/api/admin/chat-reports', headers=self.headers(41))).json()
        self.assertTrue(first['has_more'])
        self.assertEqual(len(first['items']), 50)
        cursor = first['items'][-1]['report_id']
        second = await (await self.client.get(f'/api/admin/chat-reports?before={cursor}', headers=self.headers(41))).json()
        self.assertFalse(second['has_more'])
        self.assertEqual(len(second['items']), 1)
        self.assertLess(second['items'][0]['report_id'], cursor)

    async def test_unauthorized_write_and_cross_conversation_access(self):
        cid = await self.start()
        mid = (await (await self.send(cid, 200)).json())['message_id']
        path = f'/api/chat/{cid}'
        self.assertEqual((await self.client.get(path + '/messages')).status, 401)
        headers = self.headers(200)
        headers['Origin'] = 'https://attacker.example'
        self.assertEqual((await self.client.post(path + '/messages', json={
            'text': 'forged', 'client_id': str(uuid.uuid4()), 'image': ''
        }, headers=headers)).status, 403)
        for endpoint, body in (('/read', {'through': mid}), ('/block', {'blocked': True}),
                               ('/report', {'message_id': mid, 'reason': 'تخلف'}),
                               ('/messages', {'text': 'forged', 'client_id': str(uuid.uuid4()), 'image': ''})):
            self.assertEqual((await self.client.post(path + endpoint, json=body, headers=self.headers(300))).status, 404)
        self.assertEqual((await self.client.get('/api/admin/chat-reports', headers=self.headers(42))).status, 403)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM chat_messages WHERE conversation_id=?', (cid,)).fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM chat_blocks').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM chat_reports').fetchone()[0], 0)

    async def test_v12_migration_retains_old_data_without_creating_messages(self):
        with self.db.transaction() as conn:
            for table in ('chat_notification_control', 'chat_notification_outbox', 'chat_reports', 'chat_notifications', 'chat_blocks', 'chat_reads', 'chat_messages', 'conversations'):
                conn.execute(f'DROP TABLE {table}')
            conn.execute('PRAGMA user_version=12')
        self.db.initialize()
        self.db.initialize()
        self.assertEqual(self.market.get_ad(self.ad)['status'], 'published')
        self.assertEqual(len(self.market.photos(self.ad)), 1)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM chat_messages').fetchone()[0], 0)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)

    async def test_migration_backup_restore_and_failed_notification(self):
        cid = await self.start()
        self.bot.send_message.side_effect = Forbidden('not started')
        response = await self.send(cid, 200)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['notification_queued'])
        await NotificationWorker(self.db, self.bot).run()
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM chat_notification_outbox").fetchone()[0], 'failed')
        backup = create_backup(self.db, Path(self.tmp.name)/'backups')
        restored = Database(Path(self.tmp.name)/'restored.sqlite3')
        restored.initialize()
        restore_backup(restored, backup, Path(self.tmp.name)/'safety', confirmed=True)
        restored.initialize()
        with restored.connect() as conn:
            self.assertEqual(conn.execute('SELECT body FROM chat_messages WHERE conversation_id=?', (cid,)).fetchone()[0], 'سلام')
            self.assertEqual(conn.execute('SELECT state FROM chat_notification_outbox').fetchone()[0], 'failed')
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    async def test_durable_retry_coalescing_and_cooldown(self):
        cid = await self.start()
        await self.send(cid, 200, text='اول')
        await self.send(cid, 200, text='دوم')
        worker = NotificationWorker(self.db, self.bot)
        self.bot.send_message.side_effect = NetworkError('offline')
        await worker.run()
        with self.db.connect() as conn:
            row = conn.execute('SELECT state,attempts,last_error FROM chat_notification_outbox').fetchone()
            self.assertEqual(tuple(row), ('pending', 1, 'NetworkError'))
            self.assertEqual(conn.execute('SELECT count(*) FROM chat_notification_outbox').fetchone()[0], 1)
        self.bot.send_message.side_effect = None
        await worker.run()
        self.assertEqual(self.bot.send_message.await_count, 1)
        with self.db.transaction() as conn:
            conn.execute("UPDATE chat_notification_outbox SET next_attempt='2000-01-01T00:00:00.000Z'")
        await worker.run()
        self.assertEqual(self.bot.send_message.await_count, 2)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM chat_notification_outbox').fetchone()[0], 0)
        await self.send(cid, 200, text='سوم')
        await worker.run()
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_restart_recovery_and_retry_after(self):
        cid = await self.start()
        await self.send(cid, 200)
        await self.send(cid, 100, text='پاسخ')
        worker = NotificationWorker(self.db, self.bot)
        self.bot.send_message.side_effect = RetryAfter(120)
        await worker.run()
        await worker.run()
        self.assertEqual(self.bot.send_message.await_count, 1)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT state,attempts,(julianday(next_attempt)-julianday('now'))*86400 FROM chat_notification_outbox WHERE attempts=1").fetchone()
            self.assertEqual((row[0], row[1]), ('pending', 1))
            self.assertGreater(row[2], 110)
            conn.execute("UPDATE chat_notification_outbox SET state='inflight' WHERE attempts=1")
        recover_inflight(self.db)
        with self.db.connect() as conn:
            row = conn.execute("SELECT state,last_error,(julianday(next_attempt)-julianday('now'))*86400 FROM chat_notification_outbox WHERE attempts=1").fetchone()
            self.assertEqual((row[0], row[1]), ('pending', 'interrupted'))
            self.assertGreater(row[2], 290)

    async def test_message_arriving_during_delivery_stays_queued(self):
        cid = await self.start()
        await self.send(cid, 200, text='اول')
        worker = NotificationWorker(self.db, self.bot)
        async def send_and_receive(*args):
            await self.send(cid, 200, text='دوم')
        self.bot.send_message.side_effect = send_and_receive
        await worker.run()
        with self.db.connect() as conn:
            row = conn.execute("SELECT state,latest_message_id FROM chat_notification_outbox").fetchone()
            self.assertEqual(row['state'], 'pending')
            self.assertEqual(row['latest_message_id'], conn.execute('SELECT max(message_id) FROM chat_messages').fetchone()[0])

    async def test_v13_migration_preserves_message_and_existing_cooldown(self):
        cid = await self.start()
        await self.send(cid, 200)
        with self.db.transaction() as conn:
            conn.execute('DROP TABLE chat_notification_outbox')
            conn.execute('DROP TABLE chat_notification_control')
            conn.execute("INSERT INTO chat_notifications(user_id,last_sent_at) VALUES(100,strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
            conn.execute('PRAGMA user_version=13')
        self.db.initialize()
        await self.send(cid, 200, text='بعد')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM chat_messages').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT count(*) FROM chat_notification_control').fetchone()[0], 1)
            delay = conn.execute("SELECT (julianday(next_attempt)-julianday('now'))*86400 FROM chat_notification_outbox").fetchone()[0]
            self.assertGreater(delay, 290)

    async def test_startup_schedules_worker_and_recovers_inflight(self):
        from bot import _post_init
        cid = await self.start()
        await self.send(cid, 200)
        with self.db.transaction() as conn:
            conn.execute("UPDATE chat_notification_outbox SET state='inflight'")
        app = SimpleNamespace(bot_data={'database': self.db}, bot=self.bot, job_queue=MagicMock())
        with patch('services.miniapp.start_server', new_callable=AsyncMock) as start:
            await _post_init(app)
        start.assert_awaited_once_with(app)
        self.assertIn('chat_notification_worker', app.bot_data)
        self.assertEqual({call.kwargs['name'] for call in app.job_queue.run_repeating.call_args_list},
                         {'chat-notifications', 'marketplace-maintenance'})
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT state FROM chat_notification_outbox').fetchone()[0], 'pending')
