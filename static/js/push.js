document.addEventListener('DOMContentLoaded', () => {
  const panel = document.querySelector('[data-push]');
  if (!panel) return;
  const enable = panel.querySelector('[data-push-enable]');
  const disable = panel.querySelector('[data-push-disable]');
  const status = panel.querySelector('[data-push-status]');
  const publicKey = panel.dataset.vapidPublicKey || '';
  const csrfToken = panel.dataset.csrfToken || '';

  const setStatus = (message, error = false) => {
    status.textContent = message;
    status.classList.toggle('push-status-error', error);
  };
  const decodeKey = value => {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const raw = atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from([...raw].map(char => char.charCodeAt(0)));
  };
  const supported = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  const register = () => navigator.serviceWorker.register('/service-worker.js', { scope: '/' });
  const save = subscription => fetch('/admin/api/notifications/subscription', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify(subscription.toJSON())
  }).then(response => { if (!response.ok) throw new Error('Не удалось сохранить подписку'); return response.json(); });
  const current = async () => {
    if (!supported()) return null;
    const registration = await register();
    return registration.pushManager.getSubscription();
  };
  const render = subscription => {
    enable.hidden = Boolean(subscription);
    disable.hidden = !subscription;
    if (subscription) setStatus('Это устройство подключено к push-уведомлениям.');
  };

  if (!supported()) {
    enable.disabled = true;
    setStatus('Этот браузер не поддерживает push-уведомления.', true);
    return;
  }
  if (!publicKey) {
    enable.disabled = true;
    setStatus('Сначала создайте VAPID-ключи и сохраните настройки.', true);
    return;
  }
  current().then(render).catch(() => setStatus('Не удалось проверить подписку браузера.', true));
  enable.addEventListener('click', async () => {
    enable.disabled = true;
    try {
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') throw new Error('Разрешите уведомления в настройках браузера.');
      const registration = await register();
      const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: decodeKey(publicKey) });
      await save(subscription);
      render(subscription);
      setStatus('Устройство подключено. Можно отправить тестовый push.');
    } catch (error) {
      setStatus(error.message || 'Не удалось включить уведомления.', true);
      enable.disabled = false;
    }
  });
  disable.addEventListener('click', async () => {
    disable.disabled = true;
    try {
      const subscription = await current();
      if (subscription) {
        await fetch('/admin/api/notifications/subscription', { method: 'DELETE', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify({ endpoint: subscription.endpoint }) });
        await subscription.unsubscribe();
      }
      render(null);
      setStatus('Push-уведомления отключены на этом устройстве.');
    } catch (_) {
      setStatus('Не удалось отключить уведомления.', true);
    } finally {
      disable.disabled = false;
    }
  });
});
