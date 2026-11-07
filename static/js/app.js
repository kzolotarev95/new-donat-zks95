document.addEventListener('DOMContentLoaded', () => {
  const menu = document.querySelector('.menu-toggle');
  const nav = document.querySelector('.nav-links');
  if (menu && nav) {
    menu.addEventListener('click', () => {
      const open = nav.classList.toggle('open');
      menu.setAttribute('aria-expanded', String(open));
    });
  }

  const refreshRepositories = async () => {
    const cards = document.querySelectorAll('[data-repo-card]');
    if (!cards.length) return;
    try {
      const response = await fetch('/api/repos', { headers: { Accept: 'application/json' } });
      if (!response.ok) return;
      const data = await response.json();
      data.repositories.forEach(repo => {
        document.querySelectorAll(`[data-repo-stars="${CSS.escape(repo.name)}"]`).forEach(node => {
          node.textContent = `★ ${repo.stargazers_count}`;
          node.classList.add('live-value');
        });
      });
    } catch (_) {
      // Server-rendered values remain visible when GitHub is unavailable.
    }
  };
  refreshRepositories();
  window.setInterval(refreshRepositories, 60000);

  const form = document.querySelector('#donate-form');
  if (!form) return;
  const amount = form.querySelector('[name=amount]');
  document.querySelectorAll('[data-amount]').forEach(btn => btn.addEventListener('click', () => {
    amount.value = btn.dataset.amount;
    document.querySelectorAll('[data-amount]').forEach(item => item.classList.remove('selected'));
    btn.classList.add('selected');
  }));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const status = form.querySelector('.form-status');
    const button = form.querySelector('button[type=submit]');
    status.textContent = 'Создаём защищённый платёж…';
    button.disabled = true;
    try {
      const response = await fetch('/api/payments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.fromEntries(new FormData(form)))
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Попробуйте ещё раз');
      window.location.href = data.confirmation_url;
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
});
