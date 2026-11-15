import base64
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
from services.backup import create_backup
from services.marketplace import Marketplace
import unittest
import test_miniapp as miniapp_tests


class MarketplaceWebTests(unittest.IsolatedAsyncioTestCase):
    """Exercises the real signed HTTP routes and database, without Telegram/network effects."""
    async def asyncSetUp(self):
        await miniapp_tests.ApiTests.asyncSetUp(self)
        self.bot.edit_message_text=AsyncMock();self.bot.edit_message_caption=AsyncMock()

    async def asyncTearDown(self):
        await miniapp_tests.ApiTests.asyncTearDown(self)

    def headers(self,uid=100):
        return miniapp_tests.ApiTests.headers(self,uid)

    async def put(self,body=None,uid=100):
        return await miniapp_tests.ApiTests.put(self,body,uid)

    async def publish_ad(self,owner=100,title='سامسونگ A22',city='مزارشریف',price='۲۰۰۰ افغانی',with_photo=False):
        draft=await (await self.put({**self.body,'title':title,'city':city,'price':price},owner)).json()
        if with_photo:
            data=io.BytesIO();Image.new('RGB',(12,10),'blue').save(data,'PNG')
            result=await self.client.post('/api/photo',json={'ad_id':draft['ad_id'],'image':base64.b64encode(data.getvalue()).decode()},headers=self.headers(owner))
            draft=await result.json()
        self.market.submit(draft['ad_id'],owner)
        self.market.transition(draft['ad_id'],'approved',41)
        self.market.claim_publication(draft['ad_id'],41,-1001234567890)
        self.market.finish_publication(draft['ad_id'],700+draft['ad_id'],-1001234567890)
        return draft['ad_id']

    async def test_new_endpoints_require_signed_telegram_identity(self):
        for path in ('/api/listings', '/api/ad/1', '/api/ad/1/photo/0', '/api/notifications'):
            with self.subTest(path=path):
                self.assertEqual((await self.client.get(path)).status, 401)
        for path in ('/api/favorite', '/api/preferences', '/api/notifications/read', '/api/owner-action', '/api/report'):
            with self.subTest(path=path):
                self.assertEqual((await self.client.post(path, json={})).status, 401)

    async def test_marketplace_search_filters_and_private_drafts(self):
        ad_id=await self.publish_ad(with_photo=True)
        await self.put({**self.body,'title':'پیش‌نویس پنهان'},200)
        result=await (await self.client.get('/api/listings?q=سامسونگ&city=مزارشریف&min_price=1000&max_price=3000',headers=self.headers(200))).json()
        self.assertEqual([ad_id],[a['ad_id'] for a in result['items']])
        self.assertNotIn('contact',result['items'][0]);self.assertEqual(result['items'][0]['photo_count'],1)
        response=await self.client.get('/api/listings?min_price=3000&max_price=1000',headers=self.headers());self.assertEqual(response.status,400)
        hidden=self.market.draft_for_user(200)
        response=await self.client.get('/api/ad/'+str(hidden['ad_id']),headers=self.headers(100));self.assertEqual(response.status,404)

    async def test_public_photo_survives_backup_but_archived_is_private(self):
        ad_id=await self.publish_ad(with_photo=True)
        response=await self.client.get(f'/api/ad/{ad_id}/photo/0',headers=self.headers(200));self.assertEqual(response.status,200)
        backup=create_backup(self.db,Path(self.tmp.name)/'outside')
        from database import Database
        other=Database(backup);other.initialize();self.assertEqual(self.market.photos(ad_id),Marketplace(other).photos(ad_id))
        response=await self.client.post('/api/owner-action',json={'ad_id':ad_id,'action':'archived'},headers=self.headers())
        self.assertEqual(response.status,200)
        self.assertEqual((await self.client.get(f'/api/ad/{ad_id}/photo/0',headers=self.headers(200))).status,404)
        self.assertEqual((await self.client.get(f'/api/ad/{ad_id}/photo/0',headers=self.headers())).status,200)

    async def test_third_legacy_photo_is_authenticated_and_owner_scoped(self):
        draft = await (await self.put()).json()
        ad_id = draft['ad_id']
        for file_id in ('legacy-one', 'legacy-two', 'legacy-three'):
            self.market.add_photo(ad_id, 100, file_id)
        remote = SimpleNamespace(file_size=5, download_as_bytearray=AsyncMock(return_value=bytearray(b'photo')))
        self.bot.get_file = AsyncMock(return_value=remote)
        url = f'/api/ad/{ad_id}/photo/2'
        self.assertEqual((await self.client.get(url)).status, 401)
        self.assertEqual((await self.client.get(url, headers=self.headers(200))).status, 404)
        self.bot.get_file.assert_not_awaited()
        response = await self.client.get(url, headers=self.headers())
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'photo')
        self.bot.get_file.assert_awaited_once_with('legacy-three')

    async def test_favorites_persist_are_private_and_idempotent(self):
        ad_id=await self.publish_ad()
        for _ in range(2):
            response=await self.client.post('/api/favorite',json={'ad_id':ad_id,'saved':True},headers=self.headers(200));self.assertEqual(response.status,200)
        listing=await (await self.client.get('/api/listings?favorites=1',headers=self.headers(200))).json();self.assertEqual(listing['total'],1)
        listing=await (await self.client.get('/api/listings?favorites=1',headers=self.headers(100))).json();self.assertEqual(listing['total'],0)
        from database import Database
        reopened=Database(self.db.path);reopened.initialize()
        with reopened.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM miniapp_favorites').fetchone()[0],1)
        await self.client.post('/api/favorite',json={'ad_id':ad_id,'saved':False},headers=self.headers(200))
        result=await (await self.client.get('/api/listings?favorites=1',headers=self.headers(200))).json();self.assertEqual(result['total'],0)

    async def test_owner_actions_cannot_mutate_other_user_or_skip_review(self):
        ad_id=await self.publish_ad(with_photo=True)
        response=await self.client.post('/api/owner-action',json={'ad_id':ad_id,'action':'sold'},headers=self.headers(200));self.assertEqual(response.status,404)
        response=await self.client.post('/api/owner-action',json={'ad_id':ad_id,'action':'edit'},headers=self.headers());self.assertEqual(response.status,200)
        ad=self.market.get_ad(ad_id);self.assertEqual(ad['status'],'draft');self.assertIsNone(ad['publication_message_id'])
        self.assertEqual(len(self.market.photos(ad_id)),1)
        self.assertEqual((await self.client.get(f'/api/ad/{ad_id}',headers=self.headers(200))).status,404)
        self.market.submit(ad_id,100);self.assertEqual(self.market.get_ad(ad_id)['status'],'pending')
        self.bot.edit_message_caption.assert_awaited_once()

    async def test_city_and_notifications_are_scoped(self):
        ad_id=await self.publish_ad()
        response=await self.client.post('/api/preferences',json={'city':'هرات'},headers=self.headers());self.assertEqual(response.status,200)
        state=await (await self.client.get('/api/state',headers=self.headers())).json();self.assertEqual(state['profile']['city'],'هرات')
        state=await (await self.client.get('/api/state',headers=self.headers(200))).json();self.assertEqual(state['profile']['city'],'')
        activity=await (await self.client.get('/api/notifications',headers=self.headers())).json();self.assertEqual(activity['unread'],1)
        await self.client.post('/api/notifications/read',json={'until':activity['items'][0]['updated_at']},headers=self.headers())
        activity=await (await self.client.get('/api/notifications',headers=self.headers())).json();self.assertEqual(activity['unread'],0)
        empty=await (await self.client.get('/api/notifications',headers=self.headers(200))).json();self.assertEqual(empty['items'],[])

    async def test_short_photo_ad_is_one_message_with_escaped_bold_title(self):
        from handlers.admin import _publish_ad
        from config import Config
        ad=await (await self.put({**self.body,'title':'گوشی <b> & تست'})).json()
        self.market.add_photo(ad['ad_id'],100,'telegram-file-id')
        self.market.submit(ad['ad_id'],100);self.market.transition(ad['ad_id'],'approved',41)
        self.bot.send_photo.return_value=SimpleNamespace(message_id=999)
        config=Config.from_env({'TELEGRAM_BOT_TOKEN':'12345:test','ADMIN_IDS':'41','AD_PUBLICATION_CHAT_ID':'-1001234567890'})
        context=SimpleNamespace(bot=self.bot,application=SimpleNamespace(bot_data={'marketplace':self.market,'config':config}))
        await _publish_ad(context,ad['ad_id'],41)
        self.assertEqual(self.market.get_ad(ad['ad_id'])['status'],'published')
        self.assertIn('&lt;b&gt;',self.bot.send_photo.await_args.kwargs['caption'])
        self.assertEqual(self.bot.send_photo.await_args.kwargs['parse_mode'],'HTML')
        # Only owner confirmation is a separate text message; public ad is a single photo caption.
        self.assertEqual(self.bot.send_message.await_count,1)

    async def test_wanted_preview_and_publication_keep_details_once(self):
        from config import Config
        from handlers.admin import _publish_ad

        text = 'یک گوشی سامسونگ سالم با حافظه ۱۲۸ گیگ می‌خواهم؛ بودجه ۳۰۰۰ افغانی، کابل'
        extracted = await self.client.post('/api/extract', json={'text': text, 'ad_type': 'wanted'}, headers=self.headers())
        self.assertEqual((await extracted.json())['title'], 'خریدار گوشی سامسونگ')
        response = await self.put({**self.body, 'title': text, 'description': text, 'ad_type': 'wanted', 'price': '۳۰۰۰ افغانی'})
        draft = await response.json()
        self.assertEqual(draft['public_title'], 'خریدار گوشی سامسونگ')
        self.assertEqual(draft['description'], text)
        self.assertEqual(draft['public_contact_button'], '💬 پیام به خریدار')
        self.assertTrue(draft['public_hide_contact'])

        self.market.submit(draft['ad_id'], 100)
        self.market.transition(draft['ad_id'], 'approved', 41)
        self.bot.send_message.return_value = SimpleNamespace(message_id=999)
        config = Config.from_env({'TELEGRAM_BOT_TOKEN': '12345:test', 'ADMIN_IDS': '41', 'AD_PUBLICATION_CHAT_ID': '-1001234567890'})
        context = SimpleNamespace(bot=self.bot, application=SimpleNamespace(bot_data={'marketplace': self.market, 'config': config}))
        await _publish_ad(context, draft['ad_id'], 41)
        sent = self.bot.send_message.await_args_list[0]
        message = sent.args[1]
        self.assertIn(draft['public_title'], message)
        self.assertEqual(message.count(text), 1)
        self.assertIn('💰 بودجه: ۳٬۰۰۰ افغانی', message)
        self.assertIn('📍 موقعیت: کابل', message)
        self.assertNotIn(draft['contact'], message)
        button = sent.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual(button.text, draft['public_contact_button'])
        self.assertEqual(button.url, draft['contact'])
        self.assertEqual(self.market.get_ad(draft['ad_id'])['title'], text)
        self.assertEqual(self.market.get_ad(draft['ad_id'])['description'], text)

    async def test_sale_phone_remains_visible_and_contact_button_matches_preview(self):
        from config import Config
        from handlers.admin import _publish_ad

        response = await self.put({**self.body, 'contact_mode': 'phone', 'contact': '+93700000000', 'price': '۲۰۰۰ افغانی با جورآمد'})
        draft = await response.json()
        self.assertFalse(draft['public_hide_contact'])
        self.assertEqual(draft['public_contact_button'], '💬 پیام به فروشنده')
        self.assertEqual(draft['public_price'], '۲٬۰۰۰ افغانی؛ قابل جورآمد')
        self.assertEqual(draft['price'], '2000 با جورآمد')
        response = await self.put({**self.body, 'ad_id': draft['ad_id'], 'miniapp_revision': draft['miniapp_revision'],
                                   'contact_mode': 'phone', 'contact': draft['contact'], 'price': draft['price']})
        draft = await response.json()
        self.assertEqual(draft['public_price'], '۲٬۰۰۰ افغانی؛ قابل جورآمد')
        self.market.submit(draft['ad_id'], 100)
        self.market.transition(draft['ad_id'], 'approved', 41)
        self.bot.send_message.return_value = SimpleNamespace(message_id=999)
        config = Config.from_env({'TELEGRAM_BOT_TOKEN': '12345:test', 'ADMIN_IDS': '41', 'AD_PUBLICATION_CHAT_ID': '-1001234567890'})
        context = SimpleNamespace(bot=self.bot, application=SimpleNamespace(bot_data={'marketplace': self.market, 'config': config}))
        await _publish_ad(context, draft['ad_id'], 41)
        sent = self.bot.send_message.await_args_list[0]
        self.assertIn('💰 قیمت: ' + draft['public_price'], sent.args[1])
        self.assertIn('📞 راه تماس: +93700000000', sent.args[1])
        self.assertEqual(sent.kwargs['reply_markup'].inline_keyboard[0][0].text, draft['public_contact_button'])

    async def test_v4_to_v5_migration_keeps_real_photo_bytes_and_ads(self):
        ad_id=await self.publish_ad(with_photo=True)
        before=self.market.photos(ad_id)
        with self.db.transaction() as conn:
            conn.execute('DROP TABLE miniapp_favorites')
            conn.execute('DROP TABLE miniapp_profiles')
            conn.execute('PRAGMA user_version=4')
        self.db.initialize();self.db.initialize()
        self.assertEqual(self.market.photos(ad_id),before)
        self.assertEqual(self.market.get_ad(ad_id)['status'],'published')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],5)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
