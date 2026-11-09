"use strict";
document.querySelectorAll("[data-copy-command]").forEach((button) => {
  button.addEventListener("click", async () => {
    const code = button.closest(".account-command").querySelector("code");
    const status = document.querySelector("[data-copy-status]");
    try {
      await navigator.clipboard.writeText(code.textContent);
      button.textContent = "Скопировано";
      status.textContent = "Команда скопирована. Вставьте её в SSH-терминал VPS.";
      window.setTimeout(() => { button.textContent = "Копировать"; }, 1800);
    } catch (_) {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(code);
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = "Текст выделен. Скопируйте его вручную.";
    }
  });
});
