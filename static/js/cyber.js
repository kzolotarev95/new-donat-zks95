document.addEventListener('DOMContentLoaded', () => {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const ambientToggle = document.querySelector('.cyber-ambient-toggle');
  if (ambientToggle) {
    let calm = reduceMotion;
    try { calm = localStorage.getItem('cyber-calm') === 'true' || reduceMotion; } catch (_) { /* Storage may be unavailable in a private browser. */ }
    const applyAmbient = () => {
      document.body.classList.toggle('cyber-calm', calm);
      ambientToggle.setAttribute('aria-pressed', String(calm));
      const label = calm ? 'Включить свечение и анимацию' : 'Уменьшить свечение и анимацию';
      ambientToggle.setAttribute('aria-label', label);
      ambientToggle.title = label;
    };
    applyAmbient();
    ambientToggle.addEventListener('click', () => {
      calm = !calm;
      applyAmbient();
      try { localStorage.setItem('cyber-calm', String(calm)); } catch (_) { /* Keep the current page preference. */ }
    });
  }
  const syncHeader = () => document.body.classList.toggle('is-scrolled', window.scrollY > 12);
  window.addEventListener('scroll', syncHeader, { passive: true });
  syncHeader();
  if (reduceMotion || !('IntersectionObserver' in window)) return;
  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.08 });
  document.querySelectorAll('[data-cyber-reveal]').forEach(node => {
    node.classList.add('cyber-reveal-ready');
    observer.observe(node);
  });
});
