'use strict';

async function loadAdminDashboard() {
  const tg = window.Telegram?.WebApp;
  const error = document.getElementById('error');
  const loading = document.getElementById('loading');
  const dashboard = document.getElementById('dashboard');
  const refresh = document.getElementById('refresh');
  error.hidden = true;
  loading.hidden = false;
  refresh.disabled = true;
  try {
    if (!tg?.initData) throw new Error('داشبورد را از دکمهٔ پنل مدیر در ربات تلگرام باز کنید.');
    const response = await fetch('/api/admin/summary', {
      headers: {Authorization: 'tma ' + tg.initData}, cache: 'no-store'
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'دریافت آمار انجام نشد.');
    const format = value => new Intl.NumberFormat('fa').format(value);
    for (const [id, key] of Object.entries({
      'users': 'users', 'ads': 'ads', 'pending-ads': 'pending_ads',
      'views': 'ad_views', 'reports': 'reports', 'open-reports': 'open_reports'
    })) document.getElementById(id).textContent = format(data[key]);
    const date = new Date(data.views_started_at);
    document.getElementById('views-started').textContent = new Intl.DateTimeFormat('fa', {dateStyle: 'medium', timeStyle: 'short'}).format(date);
    dashboard.hidden = false;
    await loadBanners();
    await loadUsers();
    await loadReferences();
    await loadSettings();
    await loadAds();
    await loadReports();
    await loadHistory();
  } catch (e) {
    dashboard.hidden = true;
    error.textContent = e.message || 'دریافت آمار انجام نشد؛ دوباره تلاش کنید.';
    error.hidden = false;
  } finally {
    loading.hidden = true;
    refresh.disabled = false;
  }
}

document.getElementById('refresh').addEventListener('click', loadAdminDashboard);
window.Telegram?.WebApp?.ready();
window.Telegram?.WebApp?.expand();
loadAdminDashboard();

const bannerForm = document.getElementById('banner-form');
const bannerError = document.getElementById('banner-error');

async function adminRequest(path, method = 'GET', body) {
  const response = await fetch('/api/admin/' + path, {
    method, headers: {Authorization: 'tma ' + window.Telegram.WebApp.initData,
      ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
    body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store'
  });
  if (!response.ok && !response.headers.get('content-type')?.includes('application/json'))
    throw new Error(response.status === 404 ? 'مورد پیدا نشد یا دیگر در دسترس نیست.' : 'درخواست انجام نشد.');
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'درخواست انجام نشد.');
  return result;
}

function bannerErrorMessage(error) {
  bannerError.textContent = error.message || 'درخواست انجام نشد.';
  bannerError.hidden = false;
}

function bannerButton(text, handler, disabled = false, onError = bannerErrorMessage) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = text;
  button.disabled = disabled;
  button.onclick = async () => {
    button.disabled = true;
    bannerError.hidden = true;
    try { await handler(); } catch (error) { onError(error); }
    finally { button.disabled = disabled; }
  };
  return button;
}

function localDate(seconds) {
  if (seconds === null) return '';
  const date = new Date(seconds * 1000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function editBanner(item) {
  document.getElementById('banner-id').value = item.banner_id;
  document.getElementById('banner-form-mode').textContent = 'ویرایش بنر: ' + item.title;
  document.getElementById('banner-title').value = item.title;
  document.getElementById('banner-placement').value = item.placement;
  document.getElementById('banner-url').value = item.url;
  document.getElementById('banner-start').value = localDate(item.starts_at);
  document.getElementById('banner-end').value = localDate(item.ends_at);
  document.getElementById('banner-enabled').checked = Boolean(item.enabled);
  document.getElementById('banner-image').value = '';
  bannerForm.scrollIntoView({behavior: 'smooth'});
}

async function loadBanners() {
  const list = document.getElementById('banner-list');
  try {
    const data = await adminRequest('banners');
    list.replaceChildren();
    if (!data.items.length) {
      const empty = document.createElement('p');
      empty.textContent = 'هنوز بنری ثبت نشده است.';
      list.append(empty);
    }
    for (const item of data.items) {
      const row = document.createElement('article');
      row.className = 'banner-row';
      const info = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = item.title;
      const status = document.createElement('small');
      status.textContent = (item.placement === 'home' ? 'خانه' : 'جست‌وجو') + ' · ' + (item.enabled ? 'فعال' : 'غیرفعال');
      if (item.starts_at !== null) status.textContent += ' · شروع: ' + localDate(item.starts_at).replace('T', ' ');
      if (item.ends_at !== null) status.textContent += ' · پایان: ' + localDate(item.ends_at).replace('T', ' ');
      info.append(title, status);
      const actions = document.createElement('div');
      actions.className = 'banner-actions';
      const same = data.items.filter(other => other.placement === item.placement);
      const index = same.findIndex(other => other.banner_id === item.banner_id);
      actions.append(
        bannerButton('ویرایش', () => editBanner(item)),
        bannerButton('بالا', async () => { await adminRequest(`banners/${item.banner_id}/move`, 'POST', {direction:'up'}); await loadBanners(); }, index === 0),
        bannerButton('پایین', async () => { await adminRequest(`banners/${item.banner_id}/move`, 'POST', {direction:'down'}); await loadBanners(); }, index === same.length - 1),
        bannerButton('حذف', async () => {
          if (!window.confirm('این بنر حذف شود؟')) return;
          await adminRequest(`banners/${item.banner_id}`, 'DELETE', {});
          await loadBanners();
        })
      );
      row.append(info, actions);
      list.append(row);
    }
  } catch (error) { bannerErrorMessage(error); }
}

document.getElementById('banner-clear').onclick = () => {
  bannerForm.reset();
  document.getElementById('banner-id').value = '';
  document.getElementById('banner-form-mode').textContent = 'افزودن بنر جدید';
  bannerError.hidden = true;
};

bannerForm.addEventListener('submit', async event => {
  event.preventDefault();
  const submit = bannerForm.querySelector('[type=submit]');
  submit.disabled = true;
  bannerError.hidden = true;
  try {
    const file = document.getElementById('banner-image').files[0];
    if (file && file.size > 3000000) throw new Error('تصویر باید کمتر از ۳ مگابایت باشد.');
    const image = file ? await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result.split(',')[1]);
      reader.onerror = () => reject(new Error('خواندن تصویر انجام نشد.'));
      reader.readAsDataURL(file);
    }) : '';
    const dateValue = id => {
      const value = document.getElementById(id).value;
      if (!value) return null;
      const time = new Date(value).getTime();
      if (!Number.isFinite(time)) throw new Error('تاریخ معتبر نیست.');
      return Math.floor(time / 1000);
    };
    const id = document.getElementById('banner-id').value;
    await adminRequest(id ? 'banners/' + id : 'banners', id ? 'PUT' : 'POST', {
      title: document.getElementById('banner-title').value,
      placement: document.getElementById('banner-placement').value,
      url: document.getElementById('banner-url').value,
      starts_at: dateValue('banner-start'), ends_at: dateValue('banner-end'),
      enabled: document.getElementById('banner-enabled').checked, image
    });
    document.getElementById('banner-clear').click();
    await loadBanners();
  } catch (error) { bannerErrorMessage(error); }
  finally { submit.disabled = false; }
});

let usersPage = 0;
async function loadUsers() {
  const list = document.getElementById('users-list');
  const error = document.getElementById('users-error');
  try {
    const data = await adminRequest('users?page=' + usersPage);
    list.replaceChildren();
    if (!data.items.length) list.textContent = 'کاربری ثبت نشده است.';
    for (const user of data.items) {
      const row = document.createElement('article');
      row.className = 'banner-row';
      const info = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = (user.display_name || user.username || 'کاربر') + ' · ' + user.user_id;
      const status = document.createElement('small');
      status.textContent = user.is_admin ? 'مدیر' : user.reason === null ? 'فعال' :
        user.group_synced ? 'مسدود در ربات، مینی‌اپ و گروه' : 'مسدود در ربات و مینی‌اپ؛ خطا در گروه';
      info.append(title, status);
      row.append(info);
      if (!user.is_admin) {
        const actions = document.createElement('div');
        actions.className = 'banner-actions';
        const action = async (blocked, reason = '') => {
          error.hidden = true;
          try { await adminRequest(`users/${user.user_id}/block`, 'POST', {blocked, reason}); }
          catch (e) { error.textContent = e.message; error.hidden = false; }
          await loadUsers();
        };
        if (user.reason === null || !user.group_synced) actions.append(bannerButton(
          user.reason === null ? 'مسدودسازی' : 'تلاش دوباره برای گروه',
          async () => {
            const reason = window.prompt('دلیل مسدودسازی:', user.reason || '');
            if (reason === null) return;
            await action(true, reason);
          }
        ));
        if (user.reason !== null) actions.append(bannerButton('رفع مسدودی', async () => {
          if (window.confirm('مسدودیت این کاربر برداشته شود؟')) await action(false);
        }));
        row.append(actions);
      }
      list.append(row);
    }
    document.getElementById('users-prev').disabled = usersPage === 0;
    document.getElementById('users-next').disabled = !data.has_more;
  } catch (e) {
    error.textContent = e.message || 'دریافت کاربران انجام نشد.';
    error.hidden = false;
  }
}
document.getElementById('users-prev').onclick = () => { if (usersPage > 0) { usersPage--; loadUsers(); } };
document.getElementById('users-next').onclick = () => { usersPage++; loadUsers(); };

function showSectionError(id, error) {
  const element = document.getElementById(id);
  element.textContent = error.message || 'درخواست انجام نشد.';
  element.hidden = false;
}

async function loadReferences() {
  for (const [kind, idKey] of [['categories', 'category_id'], ['provinces', 'province_id']]) {
    try {
      const data = await adminRequest('reference/' + kind);
      const list = document.getElementById(kind + '-list');
      list.replaceChildren();
      data.items.forEach((item, index) => {
        const row = document.createElement('article');
        row.className = 'banner-row';
        const title = document.createElement('strong');
        title.textContent = item.name + (item.enabled ? ' · فعال' : ' · غیرفعال');
        const actions = document.createElement('div');
        actions.className = 'banner-actions';
        const failure = error => showSectionError('reference-error', error);
        const save = async (name, enabled) => {
          await adminRequest(`reference/${kind}/${item[idKey]}`, 'PUT', {name, enabled});
          await loadReferences();
        };
        actions.append(
          bannerButton('تغییر نام', async () => {
            const name = window.prompt('نام تازه:', item.name);
            if (name !== null) await save(name, Boolean(item.enabled));
          }, false, failure),
          bannerButton(item.enabled ? 'غیرفعال' : 'فعال', () => save(item.name, !item.enabled), false, failure),
          bannerButton('بالا', async () => {await adminRequest(`reference/${kind}/${item[idKey]}/move`, 'POST', {direction:'up'}); await loadReferences();}, index === 0, failure),
          bannerButton('پایین', async () => {await adminRequest(`reference/${kind}/${item[idKey]}/move`, 'POST', {direction:'down'}); await loadReferences();}, index === data.items.length - 1, failure)
        );
        row.append(title, actions);
        list.append(row);
      });
    } catch (error) { showSectionError('reference-error', error); }
  }
}

for (const [formId, inputId, kind] of [['category-form','category-name','categories'], ['province-form','province-name','provinces']]) {
  document.getElementById(formId).addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      await adminRequest('reference/' + kind, 'POST', {name:document.getElementById(inputId).value, enabled:true});
      form.reset();
      await loadReferences();
    } catch (error) { showSectionError('reference-error', error); }
    finally { button.disabled = false; }
  });
}

async function loadSettings() {
  try {
    const data = await adminRequest('settings');
    const list = document.getElementById('settings-list');
    list.replaceChildren();
    const flags = {
      welcome_enabled:'خوش‌آمدگویی', links_allowed:'اجازهٔ پیوندها',
      join_approval:'تأیید مدیر برای درخواست عضویت', public_rankings:'رتبه‌بندی عمومی'
    };
    const groupFlags = {invite_required:'شرط دعوت گروه', ad_delete:'حذف خودکار آگهی مستقیم در گروه'};
    const textSettings = {rules:'قوانین', welcome_message:'متن خوش‌آمدگویی', blocked_phrases:'عبارت‌های مسدود'};
    const failure = error => showSectionError('settings-error', error);
    for (const [key, label] of Object.entries({...flags, ...(data.group ? groupFlags : {})})) {
      const isGroup = Object.hasOwn(groupFlags, key);
      const enabled = isGroup ? Boolean(data.group[key]) : data.values[key] === '1';
      const row = document.createElement('div');
      row.className = 'banner-row';
      const title = document.createElement('strong');
      title.textContent = label + (enabled ? ' · روشن' : ' · خاموش');
      row.append(title, bannerButton(enabled ? 'خاموش‌کردن' : 'روشن‌کردن', async () => {
        if (key === 'ad_delete' && !enabled && !window.confirm('حذف خودکار آگهی‌های مستقیم گروه روشن شود؟')) return;
        await adminRequest('settings', 'POST', {key, value:!enabled});
        await loadSettings();
      }, false, failure));
      list.append(row);
    }
    if (data.group) {
      const form = document.createElement('form');
      form.className = 'settings-item';
      const label = document.createElement('label');
      label.textContent = 'شمار دعوت لازم (۱ تا ۲۰)';
      const input = document.createElement('input');
      input.type = 'number';
      input.min = 1;
      input.max = 20;
      input.required = true;
      input.value = data.group.required_invites;
      label.append(input);
      const save = document.createElement('button');
      save.type = 'submit';
      save.textContent = 'ذخیره';
      form.append(label, save);
      form.onsubmit = async event => {
        event.preventDefault();
        save.disabled = true;
        try { await adminRequest('settings', 'POST', {key:'required_invites', value:Number(input.value)}); await loadSettings(); }
        catch (error) { failure(error); }
        finally { save.disabled = false; }
      };
      list.append(form);
    } else {
      const notice = document.createElement('p');
      notice.textContent = 'گروه تنظیم نشده است؛ تنظیمات گروه در دسترس نیست.';
      list.append(notice);
    }

    for (const [key, label] of Object.entries(textSettings)) {
      const form = document.createElement('form');
      form.className = 'settings-item';
      const title = document.createElement('label');
      title.textContent = label;
      const input = document.createElement('textarea');
      input.className = 'settings-text';
      input.maxLength = 3000;
      input.value = data.values[key];
      title.append(input);
      const save = document.createElement('button');
      save.type = 'submit';
      save.textContent = 'ذخیره';
      form.append(title, save);
      form.onsubmit = async event => {
        event.preventDefault();
        save.disabled = true;
        try { await adminRequest('settings', 'POST', {key, value:input.value}); await loadSettings(); }
        catch (error) { failure(error); }
        finally { save.disabled = false; }
      };
      list.append(form);
    }
  } catch (error) { showSectionError('settings-error', error); }
}

const adStatuses = {draft:'پیش‌نویس',pending:'در انتظار بررسی',approved:'تأییدشده',publishing:'در حال نشر',
  publication_failed:'نشر ناموفق',published:'نشرشده',rejected:'ردشده',expired:'پایان‌یافته',archived:'بایگانی',sold:'فروخته‌شده'};
let adsPage = 0, reportsPage = 0, historyPage = 0;
const adImageUrls = new Set();

async function loadAdminPhotos(ad, gallery) {
  try {
    for (let index = 0; index < ad.photo_count; index++) {
      const response = await fetch(`/api/admin/ads/${ad.ad_id}/photo/${index}`, {
        headers:{Authorization:'tma ' + window.Telegram.WebApp.initData}, cache:'no-store'
      });
      if (!response.ok) throw new Error('دریافت عکس آگهی انجام نشد.');
      const url = URL.createObjectURL(await response.blob());
      if (!gallery.isConnected) { URL.revokeObjectURL(url); return; }
      adImageUrls.add(url);
      const image = document.createElement('img');
      image.src = url;
      image.alt = 'عکس آگهی ' + ad.title;
      gallery.append(image);
    }
  } catch (error) { showSectionError('ads-error', error); }
}

async function loadAds() {
  try {
    const data = await adminRequest('ads?page=' + adsPage);
    const list = document.getElementById('ads-admin-list');
    adImageUrls.forEach(url => URL.revokeObjectURL(url));
    adImageUrls.clear();
    list.replaceChildren();
    if (!data.items.length) list.textContent = 'آگهی‌ای ثبت نشده است.';
    for (const ad of data.items) {
      const detail = document.createElement('details');
      detail.className = 'ad-admin-detail';
      const summary = document.createElement('summary');
      summary.textContent = '#' + ad.ad_id + ' · ' + (ad.title || 'پیش‌نویس') + ' · ' + (adStatuses[ad.status] || ad.status);
      const info = document.createElement('p');
      info.textContent = `کاربر: ${ad.owner_id} · ${ad.category_name} · ${ad.city}\n${ad.description}\nراه تماس: ${ad.contact}`;
      const gallery = document.createElement('div');
      gallery.className = 'admin-photo-gallery';
      let loaded = false;
      detail.addEventListener('toggle', () => {
        if (detail.open && !loaded) { loaded = true; loadAdminPhotos(ad, gallery); }
      });
      const actions = document.createElement('div');
      actions.className = 'banner-actions';
      const failure = error => showSectionError('ads-error', error);
      const run = async (action, note = '') => {
        document.getElementById('ads-error').hidden = true;
        const notice = document.getElementById('ads-notice');
        notice.hidden = true;
        try {
          const result = await adminRequest(`ads/${ad.ad_id}/action`, 'POST', {action, note});
          notice.textContent = 'وضعیت آگهی: ' + (adStatuses[result.status] || result.status) +
            (result.owner_notified === false ? '؛ اطلاع‌رسانی به صاحب آگهی ناموفق بود.' : '');
          notice.hidden = false;
        } catch (error) { failure(error); }
        await loadAds();
        await loadHistory();
      };
      if (ad.status === 'pending') {
        actions.append(
          bannerButton('تأیید و تلاش برای نشر', async () => {
            if (window.confirm('این آگهی تأیید و برای نشر ارسال شود؟')) await run('approve');
          }, false, failure),
          bannerButton('رد', async () => {
            const note = window.prompt('یادداشت رد (اختیاری):', '');
            if (note !== null) await run('reject', note);
          }, false, failure)
        );
      }
      if (['draft','pending','approved','publication_failed','published','rejected','expired','sold'].includes(ad.status))
        actions.append(bannerButton('بایگانی', async () => {
          if (window.confirm('آگهی بایگانی و از نمایش عمومی خارج شود؟')) await run('archive');
        }, false, failure));
      if (ad.status === 'archived') {
        actions.append(bannerButton('بازیابی به پیش‌نویس', async () => {
          if (window.confirm('آگهی به پیش‌نویس بازگردد؟')) await run('restore');
        }, false, failure));
        if (ad.publication_message_id) actions.append(bannerButton('همگام‌سازی گروه', () => run('sync'), false, failure));
      }
      if (ad.status === 'published') {
        const isFeatured = ad.featured_until && new Date(ad.featured_until).getTime() > Date.now();
        actions.append(bannerButton(isFeatured ? 'پایان ویژه' : 'ویژه‌کردن', async () => {
          let days = 7;
          if (!isFeatured) {
            const value = window.prompt('مدت ویژه به روز (۱ تا ۳۶۵):', '7');
            if (value === null) return;
            if (!/^[0-9]+$/.test(value)) throw new Error('مدت معتبر نیست.');
            days = Number(value);
          }
          await adminRequest(`ads/${ad.ad_id}/featured`, 'POST', {enabled:!isFeatured, days});
          await loadAds();
          await loadHistory();
        }, false, failure));
      }
      detail.append(summary, info, gallery, actions);
      list.append(detail);
    }
    document.getElementById('ads-prev').disabled = adsPage === 0;
    document.getElementById('ads-next').disabled = !data.has_more;
  } catch (error) { showSectionError('ads-error', error); }
}

async function loadReports() {
  try {
    const data = await adminRequest('reports?page=' + reportsPage);
    const list = document.getElementById('reports-list');
    list.replaceChildren();
    if (!data.items.length) list.textContent = 'گزارشی ثبت نشده است.';
    for (const report of data.items) {
      const row = document.createElement('div');
      row.className = 'banner-row';
      const info = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = `گزارش #${report.report_id} · ${report.title}`;
      const description = document.createElement('small');
      description.textContent = `${report.category} · ${report.description} · ${report.status} · گزارش‌دهنده ${report.reporter_id}`;
      info.append(title, description);
      row.append(info);
      if (report.status === 'open') {
        const actions = document.createElement('div');
        actions.className = 'banner-actions';
        const failure = error => showSectionError('reports-error', error);
        for (const [action, label] of [['review','بررسی شد'],['dismiss','رد گزارش']]) {
          actions.append(bannerButton(label, async () => {
            await adminRequest('reports/' + report.report_id, 'POST', {action});
            await loadReports();
            await loadHistory();
          }, false, failure));
        }
        row.append(actions);
      }
      list.append(row);
    }
    document.getElementById('reports-prev').disabled = reportsPage === 0;
    document.getElementById('reports-next').disabled = !data.has_more;
  } catch (error) { showSectionError('reports-error', error); }
}

async function loadHistory() {
  try {
    const data = await adminRequest('history?page=' + historyPage);
    const list = document.getElementById('history-list');
    list.replaceChildren();
    if (!data.items.length) list.textContent = 'هنوز اقدامی ثبت نشده است.';
    for (const item of data.items) {
      const row = document.createElement('div');
      row.className = 'banner-row';
      row.textContent = `${item.created_at} · مدیر ${item.admin_id} · ${item.action} · ${item.target}`;
      list.append(row);
    }
    document.getElementById('history-prev').disabled = historyPage === 0;
    document.getElementById('history-next').disabled = !data.has_more;
  } catch (error) { showSectionError('history-error', error); }
}

for (const [kind, getter, setter, load] of [
  ['ads', () => adsPage, value => adsPage = value, loadAds],
  ['reports', () => reportsPage, value => reportsPage = value, loadReports],
  ['history', () => historyPage, value => historyPage = value, loadHistory]
]) {
  document.getElementById(kind + '-prev').onclick = () => { if (getter() > 0) { setter(getter() - 1); load(); } };
  document.getElementById(kind + '-next').onclick = () => { setter(getter() + 1); load(); };
}
