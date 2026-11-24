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
