import asyncio
import base64
import io
import uuid
import unittest
from dataclasses import replace
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from telegram.error import Forbidden

import test_miniapp as fixtures
import test_marketplace_web as market_fixtures
from database import Database
from services.backup import create_backup, restore_backup
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
            for table in ('chat_reports', 'chat_notifications', 'chat_blocks', 'chat_reads', 'chat_messages', 'conversations'):
                conn.execute(f'DROP TABLE {table}')
            conn.execute('PRAGMA user_version=12')
        self.db.initialize()
        self.db.initialize()
        self.assertEqual(self.market.get_ad(self.ad)['status'], 'published')
        self.assertEqual(len(self.market.photos(self.ad)), 1)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM chat_messages').fetchone()[0], 0)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 13)

    async def test_migration_backup_restore_and_failed_notification(self):
        cid = await self.start()
        self.bot.send_message.side_effect = Forbidden('not started')
        response = await self.send(cid, 200)
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())['notified'])
        backup = create_backup(self.db, Path(self.tmp.name)/'backups')
        restored = Database(Path(self.tmp.name)/'restored.sqlite3')
        restored.initialize()
        restore_backup(restored, backup, Path(self.tmp.name)/'safety', confirmed=True)
        restored.initialize()
        with restored.connect() as conn:
            self.assertEqual(conn.execute('SELECT body FROM chat_messages WHERE conversation_id=?', (cid,)).fetchone()[0], 'سلام')
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
