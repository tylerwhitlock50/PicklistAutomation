(function () {
  const NAME = 'audit_operator_name', ACK = 'ops_operator_acknowledged_at';
  const HOURS = 10 * 60 * 60 * 1000;
  let acknowledged = 0, pending = null;
  function name() { try { return (localStorage.getItem(NAME) || '').trim(); } catch (_) { return ''; } }
  function valid() {
    let stamp = acknowledged;
    try { stamp = Number(localStorage.getItem(ACK)) || stamp; } catch (_) {}
    return !!name() && stamp > 0 && Date.now() >= stamp && Date.now() - stamp < HOURS;
  }
  function confirmIdentity() {
    if (valid()) return Promise.resolve(name());
    if (pending) return pending;
    pending = new Promise(function (resolve) {
      const dialog = document.createElement('dialog');
      dialog.className = 'operator-confirm';
      dialog.innerHTML = '<form method="dialog"><h2>Confirm your name</h2><p>Confirm who is working on this device every 10 hours.</p><label>Your name<input required maxlength="60" autocomplete="off"></label><button class="button" type="submit">Continue</button></form>';
      let input = dialog.querySelector('input');
      const roster = document.querySelector('[data-operator-picker]');
      if (roster) {
        const select = roster.cloneNode(true);
        select.removeAttribute('data-operator-picker'); select.required = true;
        input.replaceWith(select); input = select;
      }
      input.value = name();
      dialog.addEventListener('cancel', function (event) { event.preventDefault(); });
      dialog.querySelector('form').addEventListener('submit', function (event) {
        event.preventDefault();
        const value = input.value.trim();
        if (!value) { input.focus(); return; }
        const picker = document.querySelector('[data-operator-picker]');
        const free = document.querySelector('[data-operator-input]');
        if (picker) {
          const option = Array.from(picker.options).find(function (o) { return o.value.toLowerCase() === value.toLowerCase(); });
          if (!option) { input.setCustomValidity('Choose a name from the operator list at the top.'); input.reportValidity(); return; }
          picker.value = option.value;
          picker.dispatchEvent(new Event('change'));
        } else if (free) { free.value = value; free.dispatchEvent(new Event('change')); }
        acknowledged = Date.now();
        try { localStorage.setItem(NAME, picker ? picker.value : value); localStorage.setItem(ACK, String(acknowledged)); } catch (_) {}
        dialog.close(); dialog.remove(); pending = null; resolve(name());
      });
      input.addEventListener('input', function () { input.setCustomValidity(''); });
      document.body.appendChild(dialog); dialog.showModal(); input.focus(); if (input.select) input.select();
    });
    return pending;
  }
  window.OpsIdentity = { name: name, confirm: confirmIdentity };
  const originalFetch = window.fetch;
  window.fetch = async function (input, init) {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    if (url.origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const operator = await confirmIdentity();
      init = Object.assign({}, init);
      const headers = new Headers(init.headers || (input && input.headers) || {});
      headers.set('X-Operator', operator);
      init.headers = headers;
      if (typeof init.body === 'string' && (headers.get('Content-Type') || '').includes('application/json')) {
        const body = JSON.parse(init.body);
        body.operator = operator;
        init.body = JSON.stringify(body);
      }
    }
    return originalFetch(input, init);
  };
  document.addEventListener('change', function (event) {
    if (event.target.matches('[data-operator-picker], [data-operator-input]') && name()) {
      acknowledged = Date.now();
      try { localStorage.setItem(ACK, String(acknowledged)); } catch (_) {}
    }
  });
  document.addEventListener('submit', function (event) {
    const form = event.target;
    if (form.closest('.operator-confirm') || !form.matches('form[method="post"]')) return;
    if (!valid()) {
      event.preventDefault(); event.stopImmediatePropagation();
      const submitter = event.submitter;
      confirmIdentity().then(function () { form.requestSubmit(submitter || undefined); });
      return;
    }
    const fields = form.querySelectorAll('[name="operator"]');
    fields.forEach(function (field) { field.value = name(); });
  }, true);
  document.addEventListener('DOMContentLoaded', function () {
    let pickType = 'guns';
    try {
      const selected = document.querySelector('.pick-type-toggle [aria-current="page"]');
      if (selected) localStorage.setItem('ops_pick_type', new URL(selected.href).searchParams.get('pick_type'));
      const session = document.querySelector('[data-pick-type]');
      if (session && ['guns', 'components'].includes(session.dataset.pickType)) localStorage.setItem('ops_pick_type', session.dataset.pickType);
      pickType = localStorage.getItem('ops_pick_type') || 'guns';
    } catch (_) {}
    document.querySelectorAll('a[href]').forEach(function (link) {
      const url = new URL(link.href, location.href);
      if (url.origin === location.origin && url.searchParams.get('view') === 'pick' && !url.searchParams.has('pick_type')) {
        url.searchParams.set('pick_type', pickType); link.href = url.href;
      }
    });
    if (document.querySelector('.pick-start-form, .verify-start-form, #scan-input')) confirmIdentity();
  });
})();
