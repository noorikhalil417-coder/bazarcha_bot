"""Authenticated marketplace browsing and owner actions for the Telegram Mini App."""
import asyncio
import logging
import re
from datetime import UTC, datetime

from aiohttp import web
from telegram.error import TelegramError

from services.marketplace import MarketplaceError, format_price

logger = logging.getLogger(__name__)


def register_marketplace_routes(app, application):
    market = application.bot_data['marketplace']
    lock = asyncio.Lock()

    def touch(user):
        market.ensure_user(user['id'], user.get('username'), user.get('first_name',''))

    def visible(ad_id, uid):
        ad = market.get_ad(ad_id)
        if not ad or (ad['status'] != 'published' and ad['owner_id'] != uid):
            raise web.HTTPNotFound()
        return ad

    def card(row, uid):
        ad = dict(row)
        with market.db.connect() as conn:
            count = conn.execute('SELECT count(*) FROM ad_photos WHERE ad_id=?',(ad['ad_id'],)).fetchone()[0]
            favorite = conn.execute('SELECT 1 FROM miniapp_favorites WHERE user_id=? AND ad_id=?',(uid,ad['ad_id'])).fetchone() is not None
        return {**{k:ad.get(k) for k in ('ad_id','title','city','category_id','category_name','ad_type','status','updated_at')}, 'price':format_price(ad) if ad['price_minor'] is not None or ad['price_negotiable'] else 'قیمت ثبت نشده', 'price_minor':ad['price_minor'], 'photo_count':count, 'favorite':favorite,'is_owner':ad['owner_id']==uid}

    def own_cards(uid):
        with market.db.connect() as conn:
            rows = conn.execute('SELECT a.*,c.name category_name FROM ads a JOIN categories c USING(category_id) WHERE owner_id=? ORDER BY ad_id DESC LIMIT 50',(uid,)).fetchall()
        return [card(row,uid) for row in rows]

    def profile(uid):
        with market.db.connect() as conn:
            row=conn.execute('SELECT city,notifications_read_at FROM miniapp_profiles WHERE user_id=?',(uid,)).fetchone()
            saved=conn.execute('SELECT count(*) FROM miniapp_favorites f JOIN ads a ON a.ad_id=f.ad_id WHERE f.user_id=? AND a.status=\'published\'',(uid,)).fetchone()[0]
        return {**(dict(row) if row else {'city':'','notifications_read_at':'1970-01-01T00:00:00Z'}),'favorites_count':saved}

    # Keep the existing bootstrap endpoint and enrich its payload without exposing other owners' drafts.
    application.bot_data['miniapp_cards'] = own_cards
    application.bot_data['miniapp_profile'] = profile

    async def listings(request):
        uid=request['user']['id']; q=request.query
        page=max(0,min(int(q.get('page','0')),10000))
        category=int(q['category_id']) if q.get('category_id') else None
        kind=q.get('ad_type') or None
        if kind not in {None,'sale','wanted','rental','service'}: raise MarketplaceError('نوع آگهی معتبر نیست.')
        def price(name):
            raw=q.get(name,'').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789')).replace('٬','').replace(',','').strip()
            if not raw:return None
            if not re.fullmatch(r'[0-9]{1,13}',raw):raise MarketplaceError('بازهٔ قیمت معتبر نیست.')
            return int(raw)
        lower,upper=price('min_price'),price('max_price')
        if lower is not None and upper is not None and lower>upper:raise MarketplaceError('حداقل قیمت از حداکثر بیشتر است.')
        filters=dict(keyword=q.get('q','')[:80],city=q.get('city','')[:80],category_id=category,ad_type=kind,min_price=lower,max_price=upper)
        if q.get('favorites')=='1':
            clauses,params=market._search_clauses(**filters)
            with market.db.connect() as conn:
                where=' AND '.join(clauses)+ ' AND ad_id IN (SELECT ad_id FROM miniapp_favorites WHERE user_id=?)'
                total=conn.execute('SELECT count(*) FROM ads WHERE '+where,(*params,uid)).fetchone()[0]
                rows=conn.execute('SELECT ads.*,categories.name category_name FROM ads JOIN categories USING(category_id) WHERE '+where+' ORDER BY ad_id DESC LIMIT 12 OFFSET ?',(*params,uid,page*12)).fetchall()
        else:
            total=market.search_count(**filters);rows=market.search(**filters,page=page,page_size=12)
        return web.json_response({'items':[card(row,uid) for row in rows],'total':total,'page':page,'has_more':(page+1)*12<total})

    async def details(request):
        uid=request['user']['id'];ad=visible(int(request.match_info['ad_id']),uid)
        from handlers.admin import _group_message_url
        url=_group_message_url(ad['publication_chat_id'],ad['publication_message_id']) if ad['publication_chat_id'] and ad['publication_message_id'] else None
        return web.json_response({**card(ad,uid),'description':ad['description'],'contact':ad['contact'],'group_url':url,'review_note':ad['review_note'] if ad['owner_id']==uid else ''})

    async def photo(request):
        ad=visible(int(request.match_info['ad_id']),request['user']['id'])
        index=int(request.match_info['index'])
        with market.db.connect() as conn:
            row=conn.execute('SELECT p.file_id,m.data FROM ad_photos p LEFT JOIN miniapp_media m ON m.file_id=p.file_id WHERE p.ad_id=? ORDER BY p.sort_order,p.file_id LIMIT 1 OFFSET ?',(ad['ad_id'],index)).fetchone() if 0<=index<10 else None
        if not row:raise web.HTTPNotFound()
        if row['data'] is not None:content=row['data']
        else:
            remote=await application.bot.get_file(row['file_id'])
            if remote.file_size and remote.file_size>10_000_000:raise web.HTTPRequestEntityTooLarge(max_size=10_000_000,actual_size=remote.file_size)
            content=bytes(await remote.download_as_bytearray())
        return web.Response(body=content,content_type='image/jpeg')

    async def favorite(request):
        body=await request.json();uid=request['user']['id'];ad=visible(int(body['ad_id']),uid)
        if ad['status']!='published':raise MarketplaceError('فقط آگهی نشرشده را می‌توان ذخیره کرد.')
        if type(body.get('saved')) is not bool:raise ValueError
        touch(request['user'])
        with market.db.transaction() as conn:
            if body['saved']:conn.execute('INSERT OR IGNORE INTO miniapp_favorites VALUES(?,?)',(uid,ad['ad_id']))
            else:conn.execute('DELETE FROM miniapp_favorites WHERE user_id=? AND ad_id=?',(uid,ad['ad_id']))
        return web.json_response({'saved':body['saved']})

    async def preferences(request):
        body=await request.json();city=body.get('city','')
        if not isinstance(city,str) or len(city)>80:raise MarketplaceError('نام شهر معتبر نیست.')
        touch(request['user']);uid=request['user']['id']
        with market.db.transaction() as conn:
            conn.execute('INSERT INTO miniapp_profiles(user_id,city) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET city=excluded.city',(uid,city.strip()))
        return web.json_response(profile(uid))

    async def notifications(request):
        uid=request['user']['id'];read_at=profile(uid)['notifications_read_at']
        items=[{'ad_id':a['ad_id'],'title':a['title'],'status':a['status'],'updated_at':a['updated_at'],'unread':a['updated_at']>read_at} for a in own_cards(uid) if a['status']!='draft']
        return web.json_response({'items':items,'unread':sum(a['unread'] for a in items)})

    async def read_notifications(request):
        body=await request.json();uid=request['user']['id'];until=body.get('until','')
        # Mark only what was actually displayed, so later updates do not get swallowed.
        if not isinstance(until,str) or len(until)>40:raise ValueError
        try:timestamp=datetime.fromisoformat(until.replace('Z','+00:00'))
        except ValueError:raise MarketplaceError('زمان معتبر نیست.')
        if timestamp.tzinfo is None or timestamp>datetime.now(UTC):raise MarketplaceError('زمان معتبر نیست.')
        touch(request['user'])
        with market.db.transaction() as conn:
            conn.execute('INSERT OR IGNORE INTO miniapp_profiles(user_id) VALUES(?)',(uid,))
            conn.execute('UPDATE miniapp_profiles SET notifications_read_at=max(notifications_read_at,?) WHERE user_id=?',(until,uid))
        return web.json_response({'ok':True})

    async def update_group(ad,action,messages,photo_count):
        ids=[(m['chat_id'],m['message_id']) for m in messages]
        if not ids and ad['publication_chat_id'] and ad['publication_message_id']:ids=[(ad['publication_chat_id'],ad['publication_message_id'])]
        message={'sold':'✅ این آگهی فروخته شد.','archived':'این آگهی از دسترس خارج شد.','edit':'این آگهی برای اصلاح از دسترس خارج شد.'}[action]
        synced=True
        for chat_id,message_id in ids:
            try:
                if photo_count and (len(ids)==1 or (chat_id,message_id)!=ids[-1]):
                    await application.bot.edit_message_caption(chat_id=chat_id,message_id=message_id,caption=message,reply_markup=None)
                else:await application.bot.edit_message_text(chat_id=chat_id,message_id=message_id,text=message,reply_markup=None)
            except TelegramError as exc:
                synced=False;logger.info('Group status sync unavailable: %s',type(exc).__name__)
        return synced

    async def owner_action(request):
        body=await request.json();uid=request['user']['id'];ad_id=int(body['ad_id']);action=body['action']
        async with lock:
            ad=market.owned_ad(ad_id,uid)
            if not ad:raise web.HTTPNotFound()
            previous=dict(ad);messages=market.publication_messages(ad_id)
            with market.db.connect() as conn:photo_count=conn.execute('SELECT count(*) FROM ad_photos WHERE ad_id=?',(ad_id,)).fetchone()[0]
            if action=='edit':
                with market.db.transaction() as conn:
                    existing=conn.execute("SELECT 1 FROM ads WHERE owner_id=? AND status='draft' AND ad_id!=?",(uid,ad_id)).fetchone()
                    if existing:raise MarketplaceError('اول پیش‌نویس موجود را تکمیل کن.')
                    current=conn.execute('SELECT status FROM ads WHERE ad_id=? AND owner_id=?',(ad_id,uid)).fetchone()
                    if current[0] not in {'draft','published','rejected','expired','archived'}:raise MarketplaceError('این آگهی فعلاً قابل اصلاح نیست.')
                    conn.execute("UPDATE ads SET status='draft',publication_chat_id=NULL,publication_message_id=NULL,published_at=NULL,publish_claimed_at=NULL,review_note=NULL,miniapp_revision=miniapp_revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=?",(ad_id,))
                    conn.execute('DELETE FROM ad_publication_messages WHERE ad_id=?',(ad_id,))
            elif action in {'sold','archived'}:market.owner_action(ad_id,uid,action)
            else:raise MarketplaceError('این کار مجاز نیست.')
        synced=await update_group(previous,action,messages,photo_count) if previous['status']=='published' else True
        return web.json_response({'ok':True,'group_synced':synced})

    async def report(request):
        body=await request.json();uid=request['user']['id'];ad=visible(int(body['ad_id']),uid)
        if ad['status']!='published' or ad['owner_id']==uid:raise MarketplaceError('این آگهی قابل گزارش نیست.')
        touch(request['user'])
        report_id=market.add_report(ad['ad_id'],uid,'other','گزارش از مینی‌اپ')
        return web.json_response({'report_id':report_id})

    app.router.add_get('/api/listings',listings)
    app.router.add_get('/api/ad/{ad_id}',details)
    app.router.add_get('/api/ad/{ad_id}/photo/{index}',photo)
    app.router.add_post('/api/favorite',favorite)
    app.router.add_post('/api/preferences',preferences)
    app.router.add_get('/api/notifications',notifications)
    app.router.add_post('/api/notifications/read',read_notifications)
    app.router.add_post('/api/owner-action',owner_action)
    app.router.add_post('/api/report',report)
