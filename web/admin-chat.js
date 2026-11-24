'use strict';

(() => {
  const list = document.getElementById('chat-reports-list');
  const error = document.getElementById('chat-reports-error');
  const more = document.getElementById('chat-reports-more');
  const urls = new Set();
  let cursor = 0;
  function failure(e) { error.textContent = e.message || 'درخواست انجام نشد.'; error.hidden = false; }
  async function load(append = false) {
    error.hidden = true;
    const data = await adminRequest('chat-reports' + (append ? '?before=' + cursor : ''));
    if (!append) {
      urls.forEach(url => URL.revokeObjectURL(url));
      urls.clear();
      list.replaceChildren();
    }
    if (!data.items.length && !append) list.textContent = 'گزارش پیامی ثبت نشده است.';
    if (data.items.length) cursor = data.items.at(-1).report_id;
    more.hidden = !data.has_more;
    for (const report of data.items) {
      const row = document.createElement('article');
      row.className = 'banner-row';
      const info = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = `گزارش #${report.report_id} · ${report.status === 'open' ? 'باز' : 'بررسی‌شده'}`;
      const reason = document.createElement('small');
      reason.textContent = 'دلیل: ' + report.reason;
      info.append(title, reason);
      const evidence = document.createElement('div');
      const view = bannerButton('مشاهدهٔ پیام گزارش‌شده', async () => {
        const message = await adminRequest(`chat-reports/${report.report_id}`);
        evidence.replaceChildren();
        const body = document.createElement('p');
        body.textContent = message.body || 'پیام تصویری';
        evidence.append(body);
        if (message.has_image) {
          const response = await fetch(`/api/admin/chat-reports/${report.report_id}/image`, {
            headers:{Authorization:'tma ' + window.Telegram.WebApp.initData}, cache:'no-store'
          });
          if (!response.ok) throw Error('دریافت تصویر گزارش انجام نشد.');
          const url = URL.createObjectURL(await response.blob());
          urls.add(url);
          const image = document.createElement('img');
          image.src = url;
          image.alt = 'تصویر گزارش‌شده';
          image.className = 'chat-report-image';
          evidence.append(image);
        }
      }, false, failure);
      row.append(info, view);
      if (report.status === 'open') row.append(bannerButton('ثبت بررسی', async () => {
        await adminRequest(`chat-reports/${report.report_id}/review`, 'POST', {});
        await load();
        await loadHistory();
      }, false, failure));
      row.append(evidence);
      list.append(row);
    }
  }
  document.getElementById('chat-reports-refresh').onclick = () => load().catch(failure);
  more.onclick = async () => {
    more.disabled = true;
    try { await load(true); } catch (e) { failure(e); } finally { more.disabled = false; }
  };
})();
