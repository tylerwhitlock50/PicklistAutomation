(function () {
  const NAME = 'audit_operator_name', TEAM = 'ops_operator_team', ACK = 'ops_operator_acknowledged_at';
  const HOURS = 10 * 60 * 60 * 1000;
  const labels = { sales: 'Inside Sales', shipping: 'Shipping', finance: 'Finance', management: 'Management' };
  let memory = {name: '', team: '', ack: 0}, pending = null;
  function read() {
    try { memory = {name: (localStorage.getItem(NAME) || '').trim(), team: localStorage.getItem(TEAM) || '', ack: Number(localStorage.getItem(ACK)) || 0}; } catch (_) {}
    return memory;
  }
  function name() { return read().name; }
  function valid() { const who = read(); return !!who.name && who.ack > 0 && Date.now() >= who.ack && Date.now() - who.ack < HOURS; }
  function render() {
    const who = read();
    document.querySelectorAll('[data-operator-name]').forEach(el => el.textContent = who.name || 'Set your name');
    document.querySelectorAll('[data-operator-team]').forEach(el => el.textContent = labels[who.team] || 'Choose team');
    document.querySelectorAll('[data-operator-initial]').forEach(el => el.textContent = (who.name || '?').charAt(0).toUpperCase());
  }
  function save(value, team) {
    memory = {name: value, team: team, ack: Date.now()};
    try { localStorage.setItem(NAME, value); localStorage.setItem(TEAM, team); localStorage.setItem(ACK, String(memory.ack)); } catch (_) {}
    const suffix = '; path=/; max-age=31536000; SameSite=Lax';
    document.cookie = 'ops_operator=' + encodeURIComponent(value) + suffix;
    document.cookie = 'ops_operator_team=' + encodeURIComponent(team) + suffix;
    render();
    window.dispatchEvent(new CustomEvent('ops-identity-change', {detail: memory}));
  }
  function confirmIdentity(options) {
    const force = options && options.force;
    if (pending) return pending;
    if (!force && valid()) return Promise.resolve(name());
    pending = new Promise(function (resolve) {
      const prior = document.activeElement, current = read();
      const dialog = document.createElement('dialog');
      dialog.className = 'operator-confirm';
      dialog.setAttribute('aria-labelledby', 'identity-title');
      dialog.setAttribute('aria-describedby', 'identity-description');
      dialog.innerHTML = '<form method="dialog" data-no-loading><h2 id="identity-title"></h2><p id="identity-description"></p><div data-identity-fields></div><p class="muted">Team supplies your default Orders owner filter and records attribution on requests and holds. It does not restrict which available orders you can claim.</p><div class="actions"><button class="button" type="submit">Continue</button><button class="button secondary" type="button" data-cancel>Cancel</button></div></form>';
      dialog.querySelector('h2').textContent = force ? 'Edit operator identity' : (current.name ? 'Confirm it is still you' : 'Who is working on this device?');
      dialog.querySelector('#identity-description').textContent = current.name && !force ? 'Your 10-hour confirmation has expired. Confirm or change your name to continue; your work stays on this page.' : 'Your name is recorded with scans and order actions. We ask you to confirm it again after 10 hours on this device.';
      const template = document.getElementById('operator-editor-fields');
      dialog.querySelector('[data-identity-fields]').appendChild(template.content.cloneNode(true));
      const input = dialog.querySelector('[data-operator-picker], [data-operator-input]');
      const team = dialog.querySelector('[data-operator-team-picker]');
      input.value = current.name;
      if (team) team.value = current.team;
      function finish(value) { dialog.close(); dialog.remove(); pending = null; if (prior && prior.isConnected) prior.focus(); resolve(value); }
      const cancel = dialog.querySelector('[data-cancel]');
      cancel.hidden = !force;
      cancel.addEventListener('click', () => finish(current.name));
      dialog.addEventListener('cancel', function (event) { event.preventDefault(); if (force) finish(current.name); });
      dialog.querySelector('form').addEventListener('submit', function (event) {
        event.preventDefault();
        const value = input.value.trim();
        if (!value) { input.setCustomValidity('Enter your name to continue.'); input.reportValidity(); return; }
        const option = input.tagName === 'SELECT' ? input.selectedOptions[0] : null;
        save(value, option ? option.dataset.team || '' : team.value);
        finish(value);
      });
      input.addEventListener('input', () => input.setCustomValidity(''));
      document.body.appendChild(dialog); dialog.showModal(); input.focus(); if (input.select) input.select();
    });
    return pending;
  }
  window.OpsIdentity = { name: name, confirm: confirmIdentity };
  document.addEventListener('click', event => { if (event.target.closest('[data-operator-edit]')) confirmIdentity({force: true}); });
  window.addEventListener('storage', render);
  render();
  const menu = document.querySelector('.topnav-menu');
  if (menu) menu.addEventListener('click', function () {
    const expanded = menu.getAttribute('aria-expanded') !== 'true';
    menu.setAttribute('aria-expanded', String(expanded));
    menu.setAttribute('aria-label', expanded ? 'Hide navigation' : 'Show navigation');
    menu.closest('.topnav').classList.toggle('is-expanded', expanded);
  });
  const originalFetch = window.fetch;
  window.fetch = async function (input, init) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, location.href);
    const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    if (url.origin === location.origin) {
      const write = !['GET', 'HEAD', 'OPTIONS'].includes(method);
      if (write) await confirmIdentity();
      const who = read();
      if (who.name) {
        init = Object.assign({}, init);
        const headers = new Headers(init.headers || (input && input.headers) || {});
        headers.set('X-Operator', who.name);
        headers.set('X-Operator-Team', who.team);
        init.headers = headers;
        if (write && typeof init.body === 'string' && (headers.get('Content-Type') || '').includes('application/json')) {
          const body = JSON.parse(init.body); body.operator = who.name; body.operator_team = who.team; init.body = JSON.stringify(body);
        }
      }
    }
    return originalFetch(input, init);
  };
  document.addEventListener('submit', function (event) {
    const form = event.target;
    if (form.closest('.operator-confirm') || String(form.method).toLowerCase() !== 'post' || new URL(form.action, location.href).origin !== location.origin) return;
    if (!valid()) {
      event.preventDefault(); event.stopImmediatePropagation();
      const submitter = event.submitter;
      confirmIdentity().then(() => form.requestSubmit(submitter || undefined));
      return;
    }
    const who = read();
    [['operator', who.name], ['operator_team', who.team]].forEach(function ([key, value]) {
      let fields = form.querySelectorAll('[name="' + key + '"]');
      if (!fields.length) { const field = document.createElement('input'); field.type = 'hidden'; field.name = key; form.appendChild(field); fields = [field]; }
      fields.forEach(field => field.value = value);
    });
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
