'use strict';

(() => {
  let active = null, page = 0, pending = null, polling = false, firstLoad = true;
  const imageUrls = new Map();
  const originalNavigation = updateNavigation;
  updateNavigation = screen => {
    originalNavigation(screen);
    if (screen === 'conversation') document.querySelector('.bottom-nav').hidden = true;
    else if (active !== null) {
      active = null;
      pending = null;
      closeImages();
    }
  };
  const profileButton = make('button', 'glass profile-row', '💬 پیام‌ها');
  profileButton.type = 'button';
  profileButton.id = 'profile-messages';
  $('profile-ads').before(profileButton);
  const closeImages = () => {
    for (const url of imageUrls.values()) URL.revokeObjectURL(url);
    imageUrls.clear();
  };
  const originalDetail = renderDetailActions;
  renderDetailActions = ad => {
    originalDetail(ad);
    if (!ad.is_owner && ad.status === 'published') {
      $('detail-actions').prepend(actionButton('💬 گفت‌وگو در دوکانچه', async () => {
        if (demo) return toast('گفت‌وگو فقط از داخل تلگرام در دسترس است.');
        try {
          const item = await api('chat/start', 'POST', {ad_id: ad.ad_id});
          await openConversation(item.conversation_id);
        } catch (error) { toast(error.message); }
      }, 'primary'));
    }
  };

  function updateCount(count) {
    $('chat-unread').textContent = count ? `(${new Intl.NumberFormat('fa').format(count)})` : '';
    profileButton.textContent = '💬 پیام‌ها' + (count ? ` (${new Intl.NumberFormat('fa').format(count)})` : '');
  }
  async function loadInbox(append = false) {
    if (demo) {
      $('chat-list').textContent = 'برای دسترسی به پیام‌ها، برنامه را از ربات تلگرام باز کنید.';
      return;
    }
    const data = await api('chat/conversations?' + new URLSearchParams({q: $('chat-query').value, page}));
    updateCount(data.unread);
    const list = $('chat-list');
    if (!append) list.replaceChildren();
    if (!data.items.length && !append) list.textContent = 'هنوز گفت‌وگویی ندارید.';
    for (const item of data.items) {
      const button = make('button', 'glass chat-row');
      button.type = 'button';
      const avatar = make('span', 'chat-avatar', item.peer_name.slice(0, 1));
      const info = make('span', 'chat-row-info');
      info.append(make('strong', '', item.peer_name), make('small', '', item.title + ' · ' + item.price));
      const badge = make('span', 'chat-badge', item.unread ? new Intl.NumberFormat('fa').format(item.unread) : '');
      button.append(avatar, info, badge);
      button.onclick = () => openConversation(item.conversation_id).catch(error => toast(error.message));
      list.append(button);
    }
    $('chat-more').hidden = !data.has_more;
  }
  async function openInbox() {
    active = null;
    closeImages();
    page = 0;
    show('inbox');
    await loadInbox();
  }
  async function imageFor(image, url, key) {
    try {
      const cid = active;
      if (!imageUrls.has(key)) {
        const response = await fetch(url, {headers: {Authorization:'tma ' + signed}, cache:'no-store'});
        if (!response.ok) throw Error('دریافت تصویر انجام نشد.');
        const objectUrl = URL.createObjectURL(await response.blob());
        if (!image.isConnected || cid !== active || current !== 'conversation') { URL.revokeObjectURL(objectUrl); return; }
        if (imageUrls.has(key)) URL.revokeObjectURL(objectUrl);
        else imageUrls.set(key, objectUrl);
      }
      image.src = imageUrls.get(key);
      image.hidden = false;
    } catch (error) { image.alt = error.message; }
  }
  function renderMessage(item, cid) {
    const row = make('article', 'chat-message ' + (item.mine ? 'mine' : 'theirs'));
    row.dataset.messageId = item.message_id;
    if (item.body) row.append(make('p', '', item.body));
    if (item.has_image) {
      const image = document.createElement('img');
      image.alt = 'تصویر پیام';
      image.hidden = true;
      row.append(image);
      imageFor(image, `/api/chat/${cid}/image/${item.message_id}`, 'm' + item.message_id);
    }
    const meta = make('div', 'chat-meta');
    const date = new Date(item.created_at);
    meta.textContent = new Intl.DateTimeFormat('fa', {dateStyle:'short', timeStyle:'short'}).format(date) +
      (item.mine ? (item.read ? ' · خوانده شد ✓✓' : ' · ارسال شد ✓') : '');
    if (!item.mine) {
      const report = make('button', '', 'گزارش');
      report.type = 'button';
      report.onclick = async () => {
        const reason = window.prompt('دلیل گزارش این پیام:');
        if (reason === null) return;
        try {
          await api(`chat/${cid}/report`, 'POST', {message_id:item.message_id, reason});
          toast('گزارش ثبت شد؛ فقط پیام گزارش‌شده در اختیار بازبین مجاز قرار می‌گیرد.');
        } catch (error) { toast(error.message); }
      };
      meta.append(report);
    }
    row.append(meta);
    return row;
  }
  async function loadConversation(before = 0) {
    if (!active) return;
    const cid = active;
    const data = await api(`chat/${cid}/messages` + (before ? '?before=' + before : ''));
    if (cid !== active || current !== 'conversation') return;
    const info = data.conversation;
    $('chat-peer').textContent = info.peer_name;
    $('chat-ad-title').textContent = info.title;
    $('chat-ad-price').textContent = info.price;
    $('chat-status').textContent = info.blocked ? 'ارسال پیام مسدود است.' : 'گفت‌وگوی خصوصی';
    $('chat-block').textContent = info.blocked_by_me ? 'رفع مسدودی کاربر' : 'مسدودکردن کاربر';
    $('chat-form').hidden = info.blocked;
    if (info.photo_count && !$('chat-ad-image').src) imageFor($('chat-ad-image'), `/api/chat/${cid}/ad-photo`, 'ad' + cid);
    const container = $('chat-messages');
    const newest = container.lastElementChild;
    if (!before && newest && data.has_more && data.items.length &&
        Number(newest.dataset.messageId) < data.items[0].message_id) {
      container.replaceChildren();
      firstLoad = true;
    }
    const atBottom = firstLoad || container.scrollHeight - container.scrollTop - container.clientHeight < 80;
    if (before) {
      const oldHeight = container.scrollHeight;
      for (const item of [...data.items].reverse()) container.prepend(renderMessage(item, cid));
      container.scrollTop += container.scrollHeight - oldHeight;
      $('chat-older').hidden = !data.has_more;
    } else {
      for (const item of data.items) {
        const existing = container.querySelector(`[data-message-id="${item.message_id}"]`);
        if (existing) {
          const meta = existing.querySelector('.chat-meta');
          if (item.mine && item.read && !meta.textContent.includes('خوانده شد')) meta.textContent = meta.textContent.replace('ارسال شد ✓', 'خوانده شد ✓✓');
        } else container.append(renderMessage(item, cid));
      }
      if (firstLoad) $('chat-older').hidden = !data.has_more;
      if (atBottom) {
        container.scrollTop = container.scrollHeight;
        const last = data.items.at(-1);
        if (last && info.unread) {
          await api(`chat/${cid}/read`, 'POST', {through:last.message_id});
          await refreshUnread();
        }
      }
      firstLoad = false;
    }
  }
  async function openConversation(cid) {
    active = cid;
    pending = null;
    firstLoad = true;
    closeImages();
    $('chat-form').reset();
    $('chat-messages').replaceChildren();
    $('chat-ad-image').removeAttribute('src');
    $('chat-ad-image').hidden = true;
    show('conversation');
    await loadConversation();
  }
  profileButton.onclick = () => openInbox().catch(error => toast(error.message));
  $('chat-back').onclick = () => openInbox().catch(error => toast(error.message));
  document.querySelector('.chat-home').onclick = () => {active = null; closeImages(); show('profile');};
  $('chat-search').onsubmit = event => {
    event.preventDefault(); page = 0; loadInbox().catch(error => toast(error.message));
  };
  $('chat-more').onclick = () => {page++; loadInbox(true).catch(error => {page--; toast(error.message);});};
  $('chat-older').onclick = async () => {
    const oldest = $('chat-messages').firstElementChild;
    if (!oldest) return;
    $('chat-older').disabled = true;
    try { await loadConversation(Number(oldest.dataset.messageId)); }
    catch (error) { toast(error.message); }
    finally { $('chat-older').disabled = false; }
  };
  $('chat-block').onclick = async () => {
    const unblock = $('chat-block').textContent.startsWith('رفع');
    if (!window.confirm(unblock ? 'مسدودی این کاربر برداشته شود؟' : 'این کاربر مسدود شود؟')) return;
    try { await api(`chat/${active}/block`, 'POST', {blocked:!unblock}); await loadConversation(); }
    catch (error) { toast(error.message); }
  };
  $('chat-form').onsubmit = async event => {
    event.preventDefault();
    const button = $('chat-form').querySelector('[type=submit]');
    const cid = active;
    if (!cid) return;
    button.disabled = true;
    try {
      if (!pending) {
        const file = $('chat-image').files[0];
        const image = file ? await readFile(await compress(file)) : '';
        if (active !== cid || current !== 'conversation') return;
        pending = {text:$('chat-text').value, image, client_id:crypto.randomUUID()};
      }
      const result = await api(`chat/${cid}/messages`, 'POST', pending);
      if (active !== cid || current !== 'conversation') return;
      pending = null;
      $('chat-form').reset();
      await loadConversation();
      if (result.notified === false) toast('پیام ذخیره شد؛ ارسال اعلان تلگرام ناموفق بود.');
    } catch (error) { toast(error.message + (pending ? '؛ برای تلاش دوباره ارسال را بزنید.' : '')); }
    finally { button.disabled = false; }
  };
  for (const input of [$('chat-text'), $('chat-image')]) input.addEventListener('input', () => { pending = null; });
  async function refreshUnread() {
    if (!demo && !document.hidden) {
      try { updateCount((await api('chat/unread')).unread); }
      catch (error) { console.warn('دریافت شمار پیام‌ها انجام نشد:', error.message); }
    }
  }
  refreshUnread();
  setInterval(refreshUnread, 30000);
  setInterval(async () => {
    if (polling || document.hidden || !['inbox','conversation'].includes(current)) return;
    polling = true;
    try {
      if (current === 'conversation' && active) await loadConversation();
      else if (current === 'inbox' && page === 0) await loadInbox();
    } catch (error) { toast(error.message || 'به‌روزرسانی پیام‌ها انجام نشد.'); }
    finally { polling = false; }
  }, 10000);
})();
