document.addEventListener('DOMContentLoaded', () => {
  const button = document.querySelector('#download-recovery-codes');
  const codes = document.querySelector('#recovery-codes');
  if (!button || !codes) return;
  button.addEventListener('click', () => {
    const text = 'ZKS95 Developer — резервные коды 2FA\nКаждый код используется один раз. Храните в тайне.\n\n' + codes.textContent.trim() + '\n';
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'zks95-2fa-recovery-codes.txt';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
});
