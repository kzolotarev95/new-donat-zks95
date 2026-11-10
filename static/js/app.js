document.addEventListener('DOMContentLoaded', () => {
  const menu = document.querySelector('.menu-toggle');
  const nav = document.querySelector('.nav-links');
  if (menu && nav) {
    const closeMenu = () => {
      nav.classList.remove('open');
      menu.setAttribute('aria-expanded', 'false');
    };
    menu.addEventListener('click', () => {
      const open = nav.classList.toggle('open');
      menu.setAttribute('aria-expanded', String(open));
    });
    nav.querySelectorAll('a').forEach(link => link.addEventListener('click', closeMenu));
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && nav.classList.contains('open')) {
        closeMenu();
        menu.focus();
      }
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

  const donorTriggers = document.querySelectorAll('.donor-show-all, [data-show-all-donors]');
  donorTriggers.forEach(showAllDonors => {
    showAllDonors.addEventListener('click', event => {
      event.preventDefault();
      const extraDonors = document.querySelectorAll('#donor-list [data-extra-donor]');
      extraDonors.forEach(card => { card.hidden = false; });
      donorTriggers.forEach(trigger => {
        trigger.setAttribute('aria-expanded', 'true');
        trigger.hidden = true;
        if (trigger.parentElement.classList.contains('donor-more-actions')) trigger.parentElement.hidden = true;
      });
      if (extraDonors.length) {
        extraDonors[0].setAttribute('tabindex', '-1');
        extraDonors[0].focus({ preventScroll: true });
      }
    });
  });

  const form = document.querySelector('#donate-form');
  if (!form) return;
  const amount = form.querySelector('[name=amount]');
  const syncAmounts = () => {
    form.querySelectorAll('[data-amount]').forEach(item => {
      const selected = Number(item.dataset.amount) === Number(amount.value);
      item.classList.toggle('selected', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
  };
  document.querySelectorAll('[data-amount]').forEach(btn => btn.addEventListener('click', () => {
    amount.value = btn.dataset.amount;
    syncAmounts();
  }));
  amount.addEventListener('input', syncAmounts);
  const message = form.querySelector('[name=message]');
  const messageCount = form.querySelector('#message-count');
  if (message && messageCount) {
    const countMessage = () => { messageCount.textContent = `${message.value.length} / ${message.maxLength}`; };
    message.addEventListener('input', countMessage);
    countMessage();
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const status = form.querySelector('.form-status');
    const button = form.querySelector('button[type=submit]');
    status.textContent = 'Создаём защищённый платёж…';
    button.disabled = true;
    button.classList.add('is-loading');
    form.setAttribute('aria-busy', 'true');
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
      button.classList.remove('is-loading');
      form.removeAttribute('aria-busy');
    }
  });
});
