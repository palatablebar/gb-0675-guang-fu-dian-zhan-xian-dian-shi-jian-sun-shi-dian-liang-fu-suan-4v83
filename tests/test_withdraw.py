from fastapi.testclient import TestClient
from app import main
from app.engine import evaluate
import zipfile, io, json

HEADER='id,kind,device,at,power,unit,irradiance,status,start,end'
# 撤回事件覆盖两个采样区间：10:00 段仍受限(60<100)，10:10 段已有恢复记录(100=基线)。
RECOVERY_CSV='\n'.join([
 HEADER,
 'INV-A-00,TELEMETRY,INV-A,2026-10-15T10:00:00+08:00,60,kW,800,健康,,',
 'REF-A1-00,TELEMETRY,REF-A1,2026-10-15T10:00:00+08:00,100,kW,800,健康,,',
 'REF-A2-00,TELEMETRY,REF-A2,2026-10-15T10:00:00+08:00,100,kW,800,健康,,',
 'INV-A-10,TELEMETRY,INV-A,2026-10-15T10:10:00+08:00,100,kW,800,健康,,',
 'REF-A1-10,TELEMETRY,REF-A1,2026-10-15T10:10:00+08:00,100,kW,800,健康,,',
 'REF-A2-10,TELEMETRY,REF-A2,2026-10-15T10:10:00+08:00,100,kW,800,健康,,',
 'EA1,EVENT,INV-A,,,,,,2026-10-15T10:00:00+08:00,2026-10-15T10:20:00+08:00',
])
# 对照逆变器自身被事件覆盖：撤回该事件后才凑够对照。
REF_BLOCKED_CSV='\n'.join([
 HEADER,
 'INV-A-00,TELEMETRY,INV-A,2026-10-15T10:00:00+08:00,60,kW,800,健康,,',
 'REF-A1-00,TELEMETRY,REF-A1,2026-10-15T10:00:00+08:00,100,kW,800,健康,,',
 'REF-A2-00,TELEMETRY,REF-A2,2026-10-15T10:00:00+08:00,100,kW,800,健康,,',
 'EA1,EVENT,INV-A,,,,,,2026-10-15T10:00:00+08:00,2026-10-15T10:10:00+08:00',
 'ER1,EVENT,REF-A1,,,,,,2026-10-15T10:00:00+08:00,2026-10-15T10:10:00+08:00',
])

def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'test.sqlite'));return TestClient(main.app)
def make_run(c,payload):
    r=c.post('/api/runs',json=payload);assert r.status_code==200;return r.json()
def withdraw(c,rid,events,reason='调度撤回限电指令'):
    return c.post(f'/api/runs/{rid}/withdraw',json={'event_ids':events,'reason':reason})

def test_withdraw_delta_and_effective_events(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);pred=make_run(c,{'dataset':'clean'})
    r=withdraw(c,pred['manifest']['run_id'],['EA1']);assert r.status_code==200
    succ=r.json();w=succ['withdrawal']
    assert succ['manifest']['run_id']!=pred['manifest']['run_id']
    assert w['loss_before_kwh']=='26.6667' and w['loss_after_kwh']=='20.0000' and w['delta_kwh']=='-6.6667'
    assert w['withdrawn_event_ids']==['EA1'] and w['effective_event_ids']==['EA2','EB1']
    assert len(w['intervals'])==2
    first,second=w['intervals']
    assert first['disposition']=='仍受限' and first['start']<second['start']
    assert second['disposition']=='仍被有效事件覆盖'
    assert succ['balance']['loss_kwh']=='20.0000' and succ['applied'] is True
    assert succ['lineage']['is_current'] is True and succ['lineage']['root']==pred['manifest']['run_id']
    sealed=c.get('/api/runs/'+pred['manifest']['run_id']).json()
    assert sealed['balance']['loss_kwh']=='26.6667' and 'withdrawal' not in sealed

def test_recovery_and_still_limited_intervals(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);pred=make_run(c,{'csv_text':RECOVERY_CSV})
    assert pred['balance']['loss_kwh']=='6.6667'
    r=withdraw(c,pred['manifest']['run_id'],['EA1']);assert r.status_code==200
    w=r.json()['withdrawal']
    assert w['loss_before_kwh']=='6.6667' and w['loss_after_kwh']=='0.0000' and w['delta_kwh']=='-6.6667'
    by_start={i['start']:i for i in w['intervals']}
    assert len(by_start)==2
    first=by_start[min(by_start)];second=by_start[max(by_start)]
    assert first['disposition']=='仍受限' and first['loss_kwh'] is not None
    assert second['disposition']=='已恢复' and second['loss_kwh']=='0'
    assert w['effective_event_ids']==[] and w['released'] is True

def test_retry_idempotent_no_double_offset(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);pred=make_run(c,{'dataset':'clean'})
    a=withdraw(c,pred['manifest']['run_id'],['EA1']).json()
    b=withdraw(c,pred['manifest']['run_id'],['EA1']).json()
    assert a['manifest']['run_id']==b['manifest']['run_id']
    assert a['withdrawal']['voucher_id']==b['withdrawal']['voucher_id']
    assert b['applied'] is False
    assert len(c.get('/api/runs').json())==2
    current=c.get('/api/runs/'+pred['manifest']['run_id']+'/current').json()
    assert current['manifest']['run_id']==a['manifest']['run_id']
    assert current['balance']['loss_kwh']=='20.0000'

def test_retry_after_later_withdrawal_keeps_current(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);pred=make_run(c,{'dataset':'clean'})
    s1=withdraw(c,pred['manifest']['run_id'],['EA1']).json()
    s2=withdraw(c,s1['manifest']['run_id'],['EA2']).json()
    assert s2['balance']['loss_kwh']=='6.6667'
    again=withdraw(c,pred['manifest']['run_id'],['EA1']).json()
    assert again['manifest']['run_id']==s1['manifest']['run_id'] and again['applied'] is False
    current=c.get('/api/runs/'+pred['manifest']['run_id']+'/current').json()
    assert current['manifest']['run_id']==s2['manifest']['run_id']

def test_unclosed_successor_not_released(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);pred=make_run(c,{'dataset':'dirty'})
    r=withdraw(c,pred['manifest']['run_id'],['EA1']);assert r.status_code==200
    succ=r.json();w=succ['withdrawal']
    assert succ['archive']['status']=='待复核' and w['released'] is False
    assert succ['lineage']['is_current'] is False
    current=c.get('/api/runs/'+pred['manifest']['run_id']+'/current').json()
    assert current['manifest']['run_id']==pred['manifest']['run_id']
    page=c.get('/archive?run_id='+succ['manifest']['run_id']).text
    assert '未放行' in page and '撤回冲销凭证' in page
    assert '未放行' not in c.get('/archive?run_id='+pred['manifest']['run_id']).text

def test_released_page_shows_voucher_and_sealed_banner(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);pred=make_run(c,{'dataset':'clean'})
    succ=withdraw(c,pred['manifest']['run_id'],['EA1']).json()
    page=c.get('/archive?run_id='+succ['manifest']['run_id']).text
    assert '撤回冲销凭证' in page and '已放行' in page and '仍被有效事件覆盖' in page
    sealed=c.get('/archive?run_id='+pred['manifest']['run_id']).text
    assert '已封存' in sealed and succ['manifest']['run_id'] in sealed

def test_no_data_loss_and_evidence_destination(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);pred=make_run(c,{'dataset':'clean'})
    succ=withdraw(c,pred['manifest']['run_id'],['EA1']).json()
    again=c.get('/api/runs/'+pred['manifest']['run_id']).json()
    assert again['manifest']['sha256']==pred['manifest']['sha256']
    assert succ['counts']['input']==pred['counts']['input']
    assert any(r['id']=='EA1' for r in succ['records'])
    w=succ['withdrawal'];assert w['withdrawn_rows'][0]['id']=='EA1' and w['withdrawn_rows'][0]['line']==32
    z=zipfile.ZipFile(io.BytesIO(c.get('/api/export/'+succ['manifest']['run_id']).content))
    evidence=json.loads(z.read('evidence.json'))
    assert evidence['withdrawal']['voucher_id']==w['voucher_id'] and evidence['withdrawal']['delta_kwh']=='-6.6667'
    z2=zipfile.ZipFile(io.BytesIO(c.get('/api/export/'+pred['manifest']['run_id']).content))
    assert 'withdrawal' not in json.loads(z2.read('evidence.json'))

def test_withdraw_validation(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);pred=make_run(c,{'dataset':'clean'})
    assert withdraw(c,pred['manifest']['run_id'],['NOPE']).json()['code']=='UNKNOWN_REFERENCE'
    assert withdraw(c,pred['manifest']['run_id'],[]).json()['code']=='BAD_EVENTS'
    assert withdraw(c,pred['manifest']['run_id'],['EA1'],' ').json()['code']=='BAD_REASON'
    succ=withdraw(c,pred['manifest']['run_id'],['EA1']).json()
    r=withdraw(c,succ['manifest']['run_id'],['EA1']);assert r.status_code==409 and r.json()['code']=='ALREADY_WITHDRAWN'
    r=withdraw(c,pred['manifest']['run_id'],['EB1']);assert r.status_code==409 and r.json()['code']=='NOT_CURRENT'

def test_current_endpoint_404(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);assert c.get('/api/runs/none/current').status_code==404

def test_slicing_and_baseline_honor_withdrawn():
    state=evaluate({'dataset':'clean','overrides':{'withdrawn_events':['EA1','EA2','EB1']}})
    assert state['event_unions']==[] and state['balance']['loss_kwh']=='0.0000'
    blocked=evaluate({'csv_text':REF_BLOCKED_CSV})
    inv_a=[e for e in blocked['estimates'] if e['device']=='INV-A']
    assert inv_a[0]['code']=='NO_REFERENCE'
    released=evaluate({'csv_text':REF_BLOCKED_CSV,'overrides':{'withdrawn_events':['ER1']}})
    inv_a=[e for e in released['estimates'] if e['device']=='INV-A']
    assert inv_a[0]['code']=='ESTIMATED' and released['balance']['loss_kwh']=='6.6667'

def test_rerun_keeps_withdrawal_snapshot(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);pred=make_run(c,{'dataset':'clean'})
    succ=withdraw(c,pred['manifest']['run_id'],['EA1']).json()
    rerun=c.post('/api/runs/'+succ['manifest']['run_id']+'/rerun').json()
    assert rerun['manifest']['run_id']==succ['manifest']['run_id']
