'use strict';

if (!demo) {
  const slots = ['home', 'browse'].map(placement => ({
    placement, element: document.getElementById('ad-banner-' + placement), items: [], index: 0
  }));
  let refreshing = false;

  function showBanner(slot) {
    const now = Math.floor(Date.now() / 1000);
    slot.items = slot.items.filter(item => {
      if (item.ends_at === null || item.ends_at > now) return true;
      URL.revokeObjectURL(item.image);
      return false;
    });
    slot.element.replaceChildren();
    slot.element.hidden = !slot.items.length;
    if (!slot.items.length) return;
    slot.index %= slot.items.length;
    const item = slot.items[slot.index];
    const container = document.createElement(item.url ? 'a' : 'div');
    container.className = 'ad-banner-content';
    if (item.url) {
      container.href = item.url;
      container.target = '_blank';
      container.rel = 'noopener noreferrer';
    }
    const image = document.createElement('img');
    image.src = item.image;
    image.alt = item.title;
    const label = document.createElement('span');
    label.textContent = item.title;
    container.append(image, label);
    slot.element.append(container);
  }

  async function refreshBanners() {
    if (refreshing) return;
    refreshing = true;
    try {
      for (const slot of slots) {
        const data = await api('banners?placement=' + slot.placement);
        const items = [];
        try {
          for (const item of data.items) {
            const response = await fetch('/api/banners/' + item.banner_id + '/image', {
              headers: {Authorization: 'tma ' + signed}, cache: 'no-store'
            });
            if (!response.ok) throw new Error('دریافت تصویر بنر انجام نشد.');
            items.push({...item, image: URL.createObjectURL(await response.blob())});
          }
        } catch (error) {
          items.forEach(item => URL.revokeObjectURL(item.image));
          throw error;
        }
        slot.items.forEach(item => URL.revokeObjectURL(item.image));
        slot.items = items;
        slot.index = 0;
        showBanner(slot);
      }
    } catch (error) {
      for (const slot of slots) {
        slot.items.forEach(item => URL.revokeObjectURL(item.image));
        slot.items = [];
        showBanner(slot);
      }
      toast(error.message || 'دریافت بنرها انجام نشد.');
    } finally { refreshing = false; }
  }

  refreshBanners();
  setInterval(() => {
    for (const slot of slots) {
      if (slot.items.length > 1) slot.index = (slot.index + 1) % slot.items.length;
      showBanner(slot);
    }
  }, 6000);
  setInterval(refreshBanners, 60000);
}
