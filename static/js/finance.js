document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-finance-chart]').forEach(chart => {
    const source = JSON.parse(chart.querySelector('script').textContent);
    const stride = Math.max(1, Math.ceil(source.length / 120));
    const rows = [];
    for (let i = 0; i < source.length; i += stride) {
      const group = source.slice(i, i + stride);
      const row = { ...group[0] };
      row.label = group.length > 1 ? `${group[0].label} — ${group[group.length - 1].label}` : row.label;
      for (const key of ['income', 'payments', 'fees', 'payouts']) {
        row[key] = group.reduce((sum, item) => sum + (item[key] || 0), 0);
      }
      rows.push(row);
    }
    const svg = chart.querySelector('svg');
    const output = chart.querySelector('output');
    if (!rows.length) { output.textContent = 'Нет данных за период.'; return; }
    const metric = chart.dataset.metric;
    const max = Math.max(1, ...rows.map(row => row[metric]));
    const width = 580 / rows.length;
    let active = 0;
    const bars = rows.map((row, index) => {
      const bar = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      const height = Math.max(2, row[metric] / max * 160);
      bar.setAttribute('x', String(10 + width * index));
      bar.setAttribute('y', String(175 - height));
      bar.setAttribute('width', String(Math.max(.1, width - Math.min(4, width / 5))));
      bar.setAttribute('height', String(height));
      bar.setAttribute('rx', '2');
      svg.appendChild(bar);
      const show = () => select(index);
      bar.addEventListener('pointerenter', show);
      bar.addEventListener('click', show);
      return bar;
    });
    function select(index) {
      active = index;
      bars.forEach((bar, i) => bar.classList.toggle('is-active', i === index));
      const row = rows[index];
      const value = chart.dataset.unit === '₽' ? row[metric] / 100 : row[metric];
      output.textContent = `${row.label}: ${value.toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ${chart.dataset.unit}`;
    }
    chart.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      select(event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : Math.max(0, Math.min(rows.length - 1, active + (event.key === 'ArrowRight' ? 1 : -1))));
    });
    select(0);
  });
});
