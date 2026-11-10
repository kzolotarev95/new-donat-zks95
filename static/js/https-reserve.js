(() => {
  const section = document.querySelector('.https-reserve-settings');
  if (!section) return;
  const status = section.querySelector('.https-reserve-status');
  const links = section.querySelector('.https-reserve-links');
  let attempts = 0;
  async function refresh() {
    try {
      const response = await fetch(section.dataset.statusUrl, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) return;
      const data = await response.json();
      status.textContent = data.message;
      status.dataset.state = data.state;
      links.hidden = !data.enabled || !data.site_url;
      if (data.site_url) {
        section.querySelector('.https-reserve-site').href = data.site_url;
        section.querySelector('.https-reserve-admin').href = data.admin_url;
        section.querySelector('.https-reserve-address').textContent = data.site_url;
      }
      if (['pending', 'unavailable'].includes(data.state) && attempts++ < 20) window.setTimeout(refresh, 2000);
    } catch (_) {
      status.textContent = 'Не удалось обновить статус. Если порт отключён, откройте основной адрес сайта.';
    }
  }
  window.setTimeout(refresh, 1500);
})();
