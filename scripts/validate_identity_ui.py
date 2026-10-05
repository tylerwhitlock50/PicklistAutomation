"""Verify shared identity and shell in an isolated browser (no app/database)."""
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]

def main():
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'), autoescape=True)
    def url_for(endpoint, **values):
        if endpoint == 'static': return '/static/' + values['filename']
        return '/' + endpoint.replace('.', '/')
    def markup(roster=()):
        nav = env.get_template('_topnav.html').render(url_for=url_for,
            feature_flags=dict(shipping=True, audit=True, orders=True, serial=True, allocation=True, requests=True),
            operator_roster=roster, operator_teams=[('shipping', 'Shipping'), ('sales', 'Inside Sales')],
            request_badge=dict(open=0), active_nav='work', active_sub='pick')
        return '<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/styles.css"></head><body><main class="page-shell scan-workspace">' + nav + '<section class="card"><p class="helper-text">Helper on panel</p><p class="muted">Muted on panel</p><input id="scan-input"><form method="post" action="/save"><input name="operator" value="old"><button>Save</button></form></section><p class="helper-text">Helper on gradient</p><div class="audit-tile tile-missing"><span class="helper-text">Helper on alert</span></div></main></body></html>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport=dict(width=390, height=844))
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        current = [markup()]
        def serve(route):
            path = route.request.url.split('warehouse.test')[-1]
            if path.startswith('/static/'):
                source = ROOT / path.lstrip('/')
                route.fulfill(body=source.read_bytes(), content_type='text/css' if source.suffix == '.css' else 'text/javascript')
            elif 'warehouse.test' not in route.request.url: route.abort()
            elif path == '/echo': route.fulfill(json=route.request.headers)
            else: route.fulfill(body=current[0], content_type='text/html')
        page.route('**/*', serve)
        page.goto('http://warehouse.test/')
        page.locator('.operator-confirm input').fill('Tyler')
        page.locator('.operator-confirm select').select_option('shipping')
        page.get_by_role('button', name='Continue', exact=True).click()
        assert page.locator('.operator-confirm').count() == 0
        assert page.locator('[data-operator-name]').inner_text() == 'Tyler'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.get_by_role('link', name='Warehouse Ops · Today').count() == 1
        assert page.get_by_role('link', name='Settings', exact=True).is_visible()
        page.get_by_role('button', name='Show navigation', exact=True).click()
        assert page.get_by_role('link', name='Pick orders', exact=True).is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.get_by_role('button', name='Hide navigation', exact=True).click()
        # Header editing uses the very same fields and refreshes the 10-hour acknowledgement.
        page.locator('[data-operator-edit]').click()
        assert page.locator('.operator-confirm input').input_value() == 'Tyler'
        page.locator('.operator-confirm input').fill('Alex')
        page.get_by_role('button', name='Continue', exact=True).click()
        page.reload()
        assert page.locator('.operator-confirm').count() == 0
        assert page.locator('[data-operator-name]').inner_text() == 'Alex'
        headers = page.evaluate("async () => (await fetch('/echo', {method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify({operator:'stale'})})).json()")
        assert headers['x-operator'] == 'Alex' and headers['x-operator-team'] == 'shipping'
        page.evaluate("localStorage.setItem('ops_operator_acknowledged_at', String(Date.now()-36000001)); window.identityDone=false; OpsIdentity.confirm().then(()=>window.identityDone=true); void 0")
        assert page.get_by_role('heading', name='Confirm it is still you').is_visible()
        assert page.locator('.operator-confirm input').input_value() == 'Alex'
        page.get_by_role('button', name='Continue', exact=True).click()
        page.wait_for_function('window.identityDone')
        # Actual computed helper foregrounds over each rendered backdrop; gradients use their darkest stop.
        contrasts = page.locator('.helper-text, .muted, .topnav-user-role').evaluate_all('''els => els.map(el => {
          const rgb=s=>s.match(/[0-9.]+/g).slice(0,3).map(Number);
          const luminance=c=>c.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((n,v,i)=>n+v*[.2126,.7152,.0722][i],0);
          let p=el,bg; while(p){ const s=getComputedStyle(p); if(s.backgroundImage.includes('gradient')){bg=[239,231,213];break;} if(s.backgroundColor!=='rgba(0, 0, 0, 0)'){bg=rgb(s.backgroundColor);break;} p=p.parentElement; }
          const a=luminance(rgb(getComputedStyle(el).color)),b=luminance(bg||[245,240,230]);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
        })''')
        assert min(contrasts) >= 4.5, contrasts
        assert page.evaluate('getComputedStyle(document.body).minHeight') == '844px'
        for width in (320, 390, 768, 1280):
            page.set_viewport_size(dict(width=width,height=900))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        # Roster identities retain their roster-assigned team through the same editor.
        current[0] = markup([dict(name='Roster Person', team='sales')])
        page.evaluate('localStorage.clear()')
        page.reload()
        page.locator('.operator-confirm [data-operator-picker]').select_option('Roster Person')
        page.get_by_role('button', name='Continue', exact=True).click()
        assert page.locator('[data-operator-team]').inner_text() == 'Inside Sales'
        assert not errors, errors
        print(f'Identity initial/edit/renewal/roster, attribution, mobile menu, widths 320-1280, brand, viewport background passed; minimum helper contrast {min(contrasts):.2f}:1.')
        browser.close()

if __name__ == '__main__': main()
