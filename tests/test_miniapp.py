from __future__ import annotations
import base64
import hashlib
import hmac
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from config import Config
from database import Database, SCHEMA_VERSION
from services.backup import create_backup
from services.marketplace import Marketplace
from services.miniapp import create_web_app, extract_fields, validate_init_data

TOKEN = '12345:test-only'

def signed(user_id=100, issued=None, **extra):
    fields = {'auth_date': str(int(time.time()) if issued is None else issued), 'user': json.dumps({'id':user_id,'first_name':'Test','username':'test_seller'}), **extra}
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    fields['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
    return urlencode(fields)

class AuthTests(unittest.TestCase):
    def test_signature_expiry_tampering_and_duplicates(self):
        self.assertEqual(validate_init_data(signed(),TOKEN)['id'],100)
        for raw in (signed(issued=int(time.time())-3601), signed(issued=int(time.time())+100), signed().replace('test_seller','intruder'), signed()+'&auth_date=1'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_init_data(raw,TOKEN)

    def test_extraction_leaves_ambiguous_or_absent_values_empty(self):
        categories=[{'category_id':1,'name':'موبایل و تکنالوژی'}]
        result=extract_fields('سامسونگ A22، سالم، ۳۰۰۰ افغانی، مزارشریف',categories)
        self.assertEqual(result['price'],'۳۰۰۰ افغانی')
        self.assertEqual(result['city'],'مزارشریف')
        self.assertNotIn('price',extract_fields('مدل ۲۰۲۴ حافظه ۱۲۸',categories))
        self.assertNotIn('price',extract_fields('۳۰۰۰ افغانی یا ۴۰۰۰ افغانی',categories))
        self.assertNotIn('city',extract_fields('کابل و هرات',categories))

    def test_admin_dashboard_link_only_appears_in_private_chat(self):
        from handlers.admin import _dashboard
        config = Config.from_env({'TELEGRAM_BOT_TOKEN': TOKEN, 'ADMIN_IDS': '41', 'MINI_APP_URL': 'https://bazaar.example'})
        context = SimpleNamespace(application=SimpleNamespace(bot_data={'config': config}))
        private = SimpleNamespace(effective_chat=SimpleNamespace(type='private'))
        group = SimpleNamespace(effective_chat=SimpleNamespace(type='supergroup'))
        private_buttons = [button for row in _dashboard(context, private).inline_keyboard for button in row]
        group_buttons = [button for row in _dashboard(context, group).inline_keyboard for button in row]
        self.assertEqual(private_buttons[0].web_app.url, 'https://bazaar.example/admin.html')
        self.assertFalse(any(button.web_app for button in group_buttons))

class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/'data.sqlite3');self.db.initialize()
        self.market=Marketplace(self.db)
        self.config=Config.from_env({'TELEGRAM_BOT_TOKEN':TOKEN,'ADMIN_IDS':'41','MINI_APP_URL':'https://bazaar.example'})
        self.bot=SimpleNamespace(send_message=AsyncMock(),send_photo=AsyncMock(),send_media_group=AsyncMock())
        self.application=SimpleNamespace(bot_data={'marketplace':self.market,'config':self.config},bot=self.bot)
        self.client=TestClient(TestServer(create_web_app(self.application)))
        await self.client.start_server()
        self.body={'title':'گوشی','description':'سالم و آزمایشی','city':'کابل','price':'۲۰۰۰ افغانی','category_id':1,'ad_type':'sale','contact_mode':'telegram','contact_consent':True}

    async def asyncTearDown(self):
        await self.client.close();self.tmp.cleanup()

    def headers(self,uid=100):return {'Authorization':'tma '+signed(uid),'Origin':'https://bazaar.example'}
    async def put(self,body=None,uid=100):return await self.client.put('/api/draft',json=body or self.body,headers=self.headers(uid))

    async def test_auth_and_origin_are_enforced(self):
        self.assertEqual((await self.client.get('/api/state')).status,401)
        response=await self.client.put('/api/draft',json=self.body,headers={'Authorization':'tma '+signed(),'Origin':'https://evil.example'})
        self.assertEqual(response.status,403)
        self.assertEqual((await self.client.get('/')).status,200)
        self.assertEqual((await self.client.get('/../config.py')).status,404)

    async def test_approved_logo_is_served(self):
        response = await self.client.get('/approved-logo.png')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, 'image/png')
        self.assertEqual((await response.read())[:8], b'\x89PNG\r\n\x1a\n')

    async def test_atomic_draft_owner_revision_and_idempotent_submit(self):
        response=await self.put();self.assertEqual(response.status,200);draft=await response.json()
        self.assertEqual(draft['price'],'2000')
        update={**self.body,**{'ad_id':draft['ad_id'],'miniapp_revision':draft['miniapp_revision']}}
        self.assertEqual((await self.put(update,200)).status,400)
        response=await self.put(update);self.assertEqual(response.status,200);new=await response.json()
        self.assertEqual((await self.put(update)).status,400)
        stale=await self.client.post('/api/submit',json={'ad_id':new['ad_id'],'miniapp_revision':draft['miniapp_revision']},headers=self.headers())
        self.assertEqual(stale.status,400)
        for _ in range(2):
            response=await self.client.post('/api/submit',json={'ad_id':new['ad_id'],'miniapp_revision':new['miniapp_revision']},headers=self.headers())
            self.assertEqual(response.status,200)
        self.assertEqual(self.market.owned_ad(new['ad_id'],100)['status'],'pending')
        self.assertEqual(self.bot.send_message.await_count,2) # one reviewer, one owner, no duplicate sends

    async def test_photos_survive_backup_and_are_owner_scoped(self):
        draft=await (await self.put()).json()
        image=io.BytesIO();Image.new('RGB',(10,10),'blue').save(image,'PNG')
        response=await self.client.post('/api/photo',json={'ad_id':draft['ad_id'],'image':base64.b64encode(image.getvalue()).decode()},headers=self.headers())
        self.assertEqual(response.status,200);draft=await response.json();photo_id=draft['photos'][0]
        response=await self.client.get('/api/media/'+photo_id,headers=self.headers(200));self.assertEqual(response.status,404)
        response=await self.client.get('/api/media/'+photo_id,headers=self.headers());self.assertEqual(response.status,200)
        self.assertEqual(response.content_type,'image/jpeg')
        self.assertIsInstance(self.market.photos(draft['ad_id'])[0],bytes)
        backup=create_backup(self.db,Path(self.tmp.name)/'backups')
        restored=Database(backup);restored.initialize()
        self.assertEqual(Marketplace(restored).photos(draft['ad_id']),self.market.photos(draft['ad_id']))
        response=await self.client.post('/api/photo/delete',json={'file_id':photo_id},headers=self.headers(200));self.assertEqual(response.status,400)
        response=await self.client.post('/api/photo/delete',json={'file_id':photo_id},headers=self.headers());self.assertEqual(response.status,200)
        self.assertEqual(self.market.photos(draft['ad_id']),[])

    async def test_invalid_contact_photo_and_limit(self):
        response=await self.put({**self.body,'contact_mode':'phone','contact':'پیام بگذارید'});self.assertEqual(response.status,400)
        draft=await (await self.put()).json()
        response=await self.client.post('/api/photo',json={'ad_id':draft['ad_id'],'image':base64.b64encode(b'<script>test</script>').decode()},headers=self.headers());self.assertEqual(response.status,400)
        self.market.max_active_ads=1
        other=self.market.create_draft(100,1,'sale')
        for field,value in [('title','دیگر'),('description','آگهی دیگر'),('city','کابل'),('contact','@seller'),('price_minor',100)]:self.market.update_draft_field(other,100,field,value)
        self.market.submit(other,100)
        response=await self.client.post('/api/submit',json={'ad_id':draft['ad_id'],'miniapp_revision':draft['miniapp_revision']},headers=self.headers());self.assertEqual(response.status,400)

    async def test_v3_migration_preserves_ads_and_settings(self):
        draft=await (await self.put()).json()
        with self.db.transaction() as conn:
            conn.execute('DROP TABLE miniapp_media');conn.execute('ALTER TABLE ads DROP COLUMN miniapp_revision');conn.execute('PRAGMA user_version=3')
        self.db.initialize();self.db.initialize()
        self.assertEqual(self.market.owned_ad(draft['ad_id'],100)['title'],'گوشی')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
