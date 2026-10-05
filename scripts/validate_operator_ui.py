"""Exercise operator screens in Chromium against an isolated fixture database."""
import os
import sys
import tempfile
import gc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        os.environ.update(ENABLE_SCHEDULER="false", ACCESS_MODE="off", DATABASE_URL="",
                          RUN_HISTORY_DB_PATH=str(Path(temporary) / "ui.db"))
        # App startup normally initializes the configured remote audit store.
        # Fixture validation must never connect to that database.
        with patch("picklist.stores.audit_store.initialize", return_value=False):
            from picklist.app import app
        from picklist import db
        from picklist.stores import pick_store, verify_store
        from playwright.sync_api import sync_playwright
        app.config["TESTING"] = True
        db.set_setting("feature_shipping_enabled", "true")
        rows = [{"Cust Order ID": "SO-UI", "Customer ID": "UI Customer", "Part Id": "GUN-1",
                 "Location": "R01S01", "SO Qty": 1, "UPC": "123456789", "_query_type": "guns"},
                {"Cust Order ID": "SO-UI", "Customer ID": "UI Customer", "Part Id": "GUN-2",
                 "Location": "R09S05", "SO Qty": 1, "UPC": "987654321", "_query_type": "guns"}]
        sid = pick_store.start_order_session(plan_rows=rows, selected_orders=["SO-UI"],
            source_runs={"guns": 1}, operator="UI Picker", pick_type="guns")
        vid = verify_store.start_session("PL-UI", {"CUST_ORDER_ID": "SO-UI", "CUSTOMER_ID": "UI Customer"},
            [{"TRACE_ID": "SERIAL-1", "PART_ID": "GUN-1"}, {"TRACE_ID": "SERIAL-2", "PART_ID": "GUN-1"}], operator="UI Picker")
        client = app.test_client()
        queue = {"orders": [{"order_id": "SO-UI", "customer_id": "UI Customer", "guns": 2,
                            "components": 0, "units": 2, "locations": ["A01"], "claimed": False}],
                 "plan_rows": rows, "source_runs": {"guns": 1}}
        with patch("picklist.routes.shipping.build_pick_order_queue", return_value=queue), sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            def serve(route):
                url = urlsplit(route.request.url)
                if url.hostname != "warehouse.test":
                    route.abort(); return
                response = client.open(url.path + ("?" + url.query if url.query else ""),
                    method=route.request.method, data=route.request.post_data,
                    headers={key: value for key, value in route.request.headers.items()
                             if key.lower() in ('content-type', 'x-csrf-token', 'x-operator', 'x-operator-team')})
                # Fulfilled mock redirects can escape Playwright's route handler.
                # Resolve local form redirects through the fixture client instead.
                if response.status_code in (302, 303):
                    assert response.headers['Location'].startswith('/')
                    response = client.get(response.headers['Location'])
                route.fulfill(status=response.status_code, body=response.data,
                              content_type=response.content_type,
                              headers={"Location": response.headers["Location"]} if "Location" in response.headers else {})
            page.route("**/*", serve)
            page.add_init_script("""try { localStorage.setItem('audit_operator_name', 'UI Picker');
              localStorage.setItem('ops_operator_acknowledged_at', String(Date.now())); } catch (_) {}
              window.sentScans = [];
              const actualFetch = window.fetch.bind(window);
              window.fetch = async function(url, init) {
                if (!String(url).endsWith('/scan')) return actualFetch(url, init);
                const body = JSON.parse(init.body); window.sentScans.push(body);
                await new Promise(resolve => setTimeout(resolve, 100));
                const count = window.sentScans.length;
                return {ok:true, status:200, json:async () => String(url).includes('/verify/')
                  ? {result:'verified', message:'Accepted', counts:{remaining:Math.max(0,2-count),verified:count}}
                  : {result:'ok',message:'Accepted',line:{id:body.line_id,cust_order_id:'SO-UI',picked_qty:1,planned_qty:1},counts:{remaining_units:2-count,picked_units:count}}};
              };""")
            page.goto(f"http://warehouse.test/pick/session/{sid}")
            assert not page.locator("#order-input").is_visible()
            assert not page.locator("#tote-input").is_visible()
            assert page.locator("#pick-instructions").is_visible()
            assert page.locator(".pick-line-current").get_attribute("data-location") == "R09S05"
            page.locator("#location-input").fill("R01S01"); page.locator("#location-input").press("Enter")
            assert page.locator("#scan-result").inner_text().find("R09S05") >= 0
            page.locator("#location-input").fill("R09S05"); page.locator("#location-input").press("Tab")
            assert page.locator("#upc-input").evaluate("el => el === document.activeElement")
            page.locator("#upc-input").fill("987654321"); page.locator("#upc-input").press("Tab")
            assert page.locator("#scan-input").evaluate("el => el === document.activeElement")
            page.locator("#scan-input").fill("SERIAL-1"); page.locator("#scan-input").press("Tab")
            page.wait_for_function("document.getElementById('upc-input').value === '' && !document.getElementById('scan-input').readOnly")
            assert page.evaluate("sentScans[0].upc") == "987654321"
            assert page.evaluate("sentScans[0].order") == "SO-UI"
            assert page.locator(".pick-line-done").count() == 1
            assert page.locator(".pick-line-current").get_attribute("data-location") == "R01S01"
            assert page.locator("#location-input").input_value() == ""
            assert page.locator("#location-input").evaluate("el => el === document.activeElement")
            page.locator("#location-input").fill("R01S01"); page.locator("#location-input").press("Enter")
            page.locator("#upc-input").fill("GUN-1"); page.locator("#upc-input").press("Enter")
            page.locator("#scan-input").fill("SERIAL-2"); page.locator("#scan-input").press("Enter")
            page.wait_for_function("document.querySelectorAll('.pick-line-done').length === 2")
            assert page.evaluate("sentScans[1].item") == "GUN-1"
            assert page.locator("[data-complete-order]").evaluate("el => el === document.activeElement")
            output = ROOT / "ui-validation"
            output.mkdir(exist_ok=True)
            page.screenshot(path=str(output / "gun-picking.png"), full_page=True)
            page.goto(f"http://warehouse.test/verify/session/{vid}")
            page.locator('[data-operator-input]').fill('Verify Operator')
            page.locator('[data-operator-input]').press('Enter')
            page.evaluate("""() => { const input = document.getElementById('scan-input');
              for(const serial of ['SERIAL-1','SERIAL-2']) { input.value=serial; input.dispatchEvent(new KeyboardEvent('keydown',{key:serial === 'SERIAL-1' ? 'Tab' : 'Enter',bubbles:true})); }
            }""")
            page.wait_for_function("sentScans.length === 2 && !document.getElementById('complete-verify').disabled")
            assert page.evaluate("sentScans.map(s=>s.scan)") == ["SERIAL-1", "SERIAL-2"]
            assert page.evaluate("sentScans.every(s=>s.operator === 'Verify Operator')")
            assert page.locator("#scan-input").evaluate("el => el === document.activeElement")
            page.screenshot(path=str(output / "verification.png"), full_page=True)
            verify_store.record_scan(vid, 'SERIAL-1', operator='UI Picker')
            page.once('dialog', lambda dialog: dialog.accept('Box moved'))
            page.locator('#cancel-verify').click()
            page.wait_for_url(f'**/verify/session/{vid}')
            page.get_by_role('heading', name='Verification cancelled', exact=True).wait_for()
            assert page.locator('#scan-input').count() == 0
            assert verify_store.compute_counts(vid)['missing'] == 0
            assert len(verify_store.get_scans(vid)) == 1
            page.screenshot(path=str(output / 'cancelled-verification.png'), full_page=True)
            page.evaluate("localStorage.setItem('ops_operator_acknowledged_at', String(Date.now()-11*60*60*1000))")
            page.evaluate("void OpsIdentity.confirm()")
            assert page.locator("dialog.operator-confirm").is_visible()
            if page.locator("dialog.operator-confirm select").count():
                page.locator("dialog.operator-confirm select").select_option(index=1)
            page.locator("dialog.operator-confirm button").click()
            page.wait_for_function("!document.querySelector('dialog.operator-confirm')")
            page.goto("http://warehouse.test/shipping?view=pick&pick_type=guns")
            assert page.locator(".pick-type-toggle").is_visible()
            assert page.locator("#operator-name").count() == 0
            page.screenshot(path=str(output / "pick-queue.png"), full_page=True)
            page.goto("http://warehouse.test/shipping?view=pick&pick_type=components")
            page.goto(f"http://warehouse.test/verify/session/{vid}")
            assert 'pick_type=components' in page.locator('.topnav-sub', has_text='Pick orders').get_attribute('href')
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"http://warehouse.test/pick/session/{sid}")
            page.screenshot(path=str(output / "gun-picking-mobile.png"), full_page=True)
            overflow = page.evaluate("""() => Array.from(document.querySelectorAll('body *')).filter(el=>el.getBoundingClientRect().right>innerWidth+1).map(el=>el.className).slice(0,12)""")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), overflow
            old = (datetime.now(timezone.utc) - timedelta(hours=11)).isoformat()
            with db.get_sqlite_conn() as connection:
                connection.execute('UPDATE pick_sessions SET started_at = ? WHERE id = ?', (old, sid))
                connection.execute('UPDATE pick_order_events SET created_at = ? WHERE session_id = ?', (old, sid))
            with patch.object(verify_store, '_now_iso', return_value=old):
                verify_store.start_session('PL-IDLE', {}, [{'TRACE_ID':'SERIAL-IDLE'}], operator='UI Picker')
            page.set_viewport_size({'width': 1280, 'height': 900})
            page.goto('http://warehouse.test/work')
            assert page.locator('.unfinished-stale').count() == 2
            page.screenshot(path=str(output / 'unfinished-work.png'), full_page=True)
            form = page.locator('[data-unfinished-close="pick"]')
            form.locator('..').locator('summary').click()
            form.locator('[name="reason"]').fill('Cart cleared')
            page.once('dialog', lambda dialog: dialog.accept())
            form.get_by_role('button', name='Close pick', exact=True).click()
            page.wait_for_function("document.readyState === 'complete' && !!document.getElementById('unfinished-work') && !document.querySelector('[data-unfinished-close=pick]')")
            assert pick_store.get_session(sid)['status'] == 'abandoned'
            assert not errors, errors
            browser.close()
        gc.collect()
        print("Operator UI checks passed: gun pairing, scan queue, identity expiry, team toggle, mobile width, cancellation and unfinished-work cleanup.")


if __name__ == "__main__":
    main()

