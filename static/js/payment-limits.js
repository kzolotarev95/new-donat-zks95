(() => {
  const form = document.querySelector('.payment-limits-form');
  if (!form) return;
  const floor = Number(form.dataset.floor);
  const ceiling = Number(form.dataset.ceiling);
  const fields = {};
  form.querySelectorAll('[data-limit]').forEach(control => {
    fields[control.dataset.limit] = {
      number: control.querySelector('input[type=number]'),
      range: control.querySelector('input[type=range]')
    };
  });
  const values = { minimum: Number(fields.minimum.number.value), maximum: Number(fields.maximum.number.value) };
  const initial = { ...values };
  const format = value => new Intl.NumberFormat('ru-RU').format(value);
  const update = () => {
    Object.entries(fields).forEach(([key, { number, range }]) => {
      if (number.value !== String(values[key])) number.value = String(values[key]);
      number.setCustomValidity('');
      range.value = String(values[key]);
      range.setAttribute('aria-valuetext', `${format(values[key])} рублей`);
      range.style.setProperty('--range-progress', `${100 * (values[key] - floor) / (ceiling - floor)}%`);
      range.disabled = false;
    });
    form.querySelector('.payment-limits-summary').textContent = `Диапазон: от ${format(values.minimum)} до ${format(values.maximum)} ₽`;
    form.querySelector('.payment-limits-pending').hidden = values.minimum === initial.minimum && values.maximum === initial.maximum;
  };
  Object.entries(fields).forEach(([key, { number, range }]) => {
    [number, range].forEach(input => input.addEventListener('input', () => {
      const value = input.valueAsNumber;
      if (!Number.isInteger(value) || value < floor || value > ceiling) {
        number.setCustomValidity(`Введите целую сумму от ${format(floor)} до ${format(ceiling)} ₽.`);
        return;
      }
      values[key] = value;
      if (values.minimum > values.maximum) values[key === 'minimum' ? 'maximum' : 'minimum'] = value;
      update();
    }));
  });
  update();
})();
