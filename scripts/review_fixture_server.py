"""Disposable local UI fixture. No ERP/network calls; only seeded scans may mutate state."""
import os
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
fixture_dir = ROOT / 'ui-validation' / ('fixture-' + uuid4().hex)
fixture_dir.mkdir(parents=True)
os.environ.update(ENABLE_SCHEDULER='false', ACCESS_MODE='off', DATABASE_URL='',
                  RUN_HISTORY_DB_PATH=str(fixture_dir / 'fixture.db'))
with patch('picklist.stores.audit_store.initialize', return_value=False):
    from picklist.app import app
from flask import abort
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
from picklist import db
from picklist.stores import pick_store, verify_store
from picklist.routes import pick, verify, shipping, settings

db.set_setting('feature_shipping_enabled', 'true')
db.set_setting('feature_allocation_enabled', 'true')
rows = [dict(zip(('Cust Order ID', 'Customer ID', 'Part Id', 'Location', 'SO Qty', 'UPC', '_query_type'), values))
        for values in [('SO-UI', 'Fixture Customer', 'GUN-1', 'R01S01', 1, '123456789', 'guns'),
                       ('SO-UI', 'Fixture Customer', 'GUN-2', 'R09S05', 1, '987654321', 'guns')]]
sid = pick_store.start_order_session(plan_rows=rows, selected_orders=['SO-UI'], source_runs={'guns': 1}, operator='UI Picker', pick_type='guns')
vid = verify_store.start_session('PL-UI', {'CUST_ORDER_ID':'SO-UI', 'CUSTOMER_ID':'Fixture Customer'},
                               [{'TRACE_ID':'SERIAL-1','PART_ID':'GUN-1'}, {'TRACE_ID':'SERIAL-2','PART_ID':'GUN-2'}], operator='UI Picker')

component_rows = [{'Cust Order ID': order, 'Customer ID': 'Fixture Components', 'Part Id': part,
                   'Location': 'BIN-A', 'SO Qty': 1, '_query_type': 'components'}
                  for order, part in [('SO-COMP-1', 'COMP-1'), ('SO-COMP-2', 'COMP-2')]]
csid = pick_store.start_order_session(plan_rows=component_rows, selected_orders=['SO-COMP-1','SO-COMP-2'],
                                    source_runs={'components': 2}, operator='UI Picker', pick_type='components')
cancel_vid = verify_store.start_session('PL-CANCEL', {'CUST_ORDER_ID':'SO-UI'},
                                      [{'TRACE_ID':'SERIAL-CANCEL','PART_ID':'GUN-1'}], operator='UI Picker')
issue_vid = verify_store.start_session('PL-ISSUE', {'CUST_ORDER_ID':'SO-UI'},
                                     [{'TRACE_ID':'SERIAL-MISSING','PART_ID':'GUN-1'}], operator='UI Picker')

def resolve_fixture_serial(scan, lines):
    if scan in {'COMP-1','COMP-2'}:
        return None, [{'part_id':scan, 'locations':[]}], False
    match = {'SERIAL-1': ('GUN-1','R01S01'), 'SERIAL-2': ('GUN-2','R09S05')}.get(scan)
    return (scan, [{'part_id': match[0], 'locations':[match[1]]}] if match else [], False)
pick._resolve_pick_candidates = resolve_fixture_serial
# Unknown UPCs must fail locally rather than query ERP.
def no_erp(*args, **kwargs):
    raise RuntimeError('ERP is unavailable in the isolated UI fixture')
pick.run_erp_query_file = no_erp
verify.run_erp_query_file = no_erp
verify._lookup_serial_shipment = lambda scan, packlist: ({'PACKLIST_ID':'PL-OTHER','CUSTOMER_NAME':'Other fixture customer'}, True) if scan == 'WRONG-BOX' else (None, True)

# Read-only queues exercise the actual route/template with deterministic saved-plan data.
def fixture_pick_queue(pick_type='guns'):
    component = pick_type == 'components'
    orders = [dict(order_id=order, customer_id=customer, customer_name=customer,
                   guns=0 if component else 2, components=2 if component else 0,
                   units=2, locations=['BIN-A' if component else 'R09S05'], claimed=claimed,
                   desired_ship_date=due, urgency=urgency)
              for order, customer, claimed, due, urgency in [
                  ('EDI-00854','Fixture overdue',False,'2026-09-01','Overdue by 34 days'),
                  ('WEB-00200','Fixture due today',False,'2026-10-05','Due today'),
                  ('SO-FUTURE','Fixture future',False,'2026-11-01',None),
                  ('SO-UI','Fixture claimed',True,'2026-10-01','Overdue by 4 days')]]
    return dict(orders=orders, plan_rows=[], source_runs={pick_type:1},
                sources=[dict(type=pick_type,id=1,timestamp='2026-10-02T12:00:00Z',age_hours=72,stale=True)])
shipping.build_pick_order_queue = fixture_pick_queue
shipping.readiness_store.latest_snapshot = lambda: dict(evaluated_at='2026-10-05T12:00:00Z',error=None,
    orders=[dict(order_id='EDI-00854',state='BLOCKED',holds=[dict(label='Fixture stock concern')])])
settings.settings_access_granted = lambda: False


@app.before_request
def fixture_only():
    from flask import request
    allowed = {'fixture_home','fixture_audit','static','pick.pick_session_page','verify.verify_session_page',
               'pick.api_pick_scan','pick.api_pick_complete','verify.api_verify_scan',
               'verify.api_verify_complete','verify.api_verify_cancel','shipping.shipping_page',
               'lookup.lookup_page','settings.settings','runs.index','shipping.work_page',
               'allocation.allocation_page','allocation.api_allocation_parts','allocation.api_allocation_detail',
               'allocation.api_allocation_history','allocation.api_allocation_preview','allocation.api_allocation_suggest',
               'allocation.api_allocation_save'}
    if request.endpoint not in allowed:
        abort(403, 'This isolated fixture only permits the seeded session workflows.')
    if request.endpoint == 'shipping.shipping_page' and request.args.get('view') != 'pick':
        abort(403, 'Only the pick queue is seeded in this fixture.')
    if request.endpoint == 'settings.settings' and request.method != 'GET':
        abort(403, 'Settings writes are disabled in this fixture.')
    if request.endpoint.startswith('pick.') and request.view_args.get('session_id') not in {sid, csid}:
        abort(403)
    if request.endpoint.startswith('verify.') and request.view_args.get('session_id') not in {vid, cancel_vid, issue_vid}:
        abort(403)

@app.get('/fixture')
def fixture_home():
    return f'<h1>Isolated UI fixture</h1><p>Use operator UI Picker. Only seeded local fixture workflows are enabled.</p><a href="/pick/session/{sid}">Pick SO-UI</a> <a href="/verify/session/{vid}">Verify PL-UI</a> <a href="/pick/session/{csid}">Component totes</a> <a href="/verify/session/{cancel_vid}">Cancel verification</a> <a href="/verify/session/{issue_vid}">Verification issues</a> <a href="/fixture/audit">Audit dashboard</a> <a href="/shipping?view=pick">Pick queue</a> <a href="/lookup">Lookup</a> <a href="/settings">Settings</a> <a href="/">Run picklist</a> <a href="/work">Today</a> <a href="/allocation?part=FIXTURE-PART">Allocation fixture</a><p>Allocation reason: start with conflict for 409, fail for 500, anything else simulates success in memory only.</p>'

@app.get('/fixture/audit')
def fixture_audit():
    from flask import render_template
    location = dict(location_id='INTERNATIONAL', warehouse_id='SHIPPING', description='International staging',
                    serial_count=3, last_inventoried_display='September 1, 2026', days_since=34,
                    cadence_days=7, due=True, last_accuracy=100)
    return render_template('audit.html', audit_available=True, sync_error=None,
                           active_sessions=[dict(id=337, label='SHIPPING / INTERNATIONAL', operator='UI Picker', done_units=1, planned_units=3, started_at='2026-10-05T10:00:00Z')],
                           due_locations=[location], warehouses=[dict(warehouse_id='SHIPPING', serial_total=3, locations=[location])],
                           recent_sessions=[], tied_row=None, last_synced_display='October 5, 2026', today_iso='2026-10-05')

# Allocation endpoints are replaced entirely: no real ERP adapter can be reached.
from flask import jsonify, request
allocation_value = '2026-10-20'

def fixture_allocation_payload(value=None, preview=False):
    effective = value if preview else allocation_value
    dates = dict(line_promise_del=effective, hdr_promise_del='2026-10-25',
                 eff_promise_del=effective or '2026-10-25', promise_del_source='line' if effective else 'header',
                 eff_promise_ship='2026-10-18', line_desired='2026-10-15', overridden=preview)
    line = dict(position=1 if preview else 2, so='SO-ALLOC', line_no=1,
                customer_id='FIXTURE', customer_name='Isolated allocation customer',
                order_date='2026-09-01', order_qty=2, shipped_qty=0, open_qty=2, dates=dates,
                eligible=True, in_picklist_window=True, linked_qty=0, reasons=[],
                allocations=[{'qty':2,'class':'ON_HAND','supply_id':'FIXTURE-STOCK','date':'2026-10-05','certainty':'available'}],
                supply_status='COVERED', covered_qty=2, est_available='2026-10-05', est_certainty='available')
    return dict(part_id='FIXTURE-PART', today='2026-10-05', lookahead_days=30, picklist_through_date='2026-11-04',
                supply=dict(events=[{'seq':1,'class':'ON_HAND','supply_id':'FIXTURE-STOCK','date':'2026-10-05',
                                     'qty':4,'allocated_qty':2,'remaining_qty':2,'certainty':'available','detail':'Fixture only'}],
                            informational=[], netting_fence=None, excluded_mps_qty=0, total_eligible_qty=4),
                demand=dict(lines=[line], eligible_open_units=2, unallocated_units=0))

def fixture_allocation_detail(part_id):
    if part_id != 'FIXTURE-PART': return jsonify(message='Use FIXTURE-PART in this isolated fixture.'), 404
    return jsonify(fixture_allocation_payload())

def fixture_allocation_preview():
    body = request.get_json() or {}
    return jsonify(baseline_position=2, new_position=1, displaced=[],
                   result=fixture_allocation_payload(body.get('new_value'), preview=True))

def fixture_allocation_save():
    global allocation_value
    body = request.get_json() or {}
    reason = str(body.get('reason') or '').lower()
    if reason.startswith('conflict'):
        allocation_value = '2026-10-12'
        return jsonify(message='Fixture conflict: another operator changed the date.', current_value=allocation_value), 409
    if reason.startswith('fail'):
        return jsonify(message='Fixture save failed. Your proposal can be retried.'), 500
    allocation_value = body.get('new_value')
    return jsonify(new_value=allocation_value, position_before=2, position_after=1,
                   warning=None, result=fixture_allocation_payload())

app.view_functions['allocation.api_allocation_detail'] = fixture_allocation_detail
app.view_functions['allocation.api_allocation_preview'] = fixture_allocation_preview
app.view_functions['allocation.api_allocation_save'] = fixture_allocation_save
app.view_functions['allocation.api_allocation_parts'] = lambda: jsonify(parts=[dict(part_id='FIXTURE-PART', description='Isolated fixture', on_hand=4, open_demand=2)])
app.view_functions['allocation.api_allocation_history'] = lambda: jsonify(changes=[])
app.view_functions['allocation.api_allocation_suggest'] = lambda: jsonify(suggested_date='2026-10-10', predicted_position=1)

if __name__ == '__main__':
    print(f'Fixture directory: {fixture_dir}', flush=True)
    print(f'Pick: http://127.0.0.1:8082/pick/session/{sid}; Verify: http://127.0.0.1:8082/verify/session/{vid}', flush=True)
    app.run(host='127.0.0.1', port=8082, debug=False, use_reloader=False)
