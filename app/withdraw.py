import hashlib
from datetime import datetime, timezone
from app.config import RULE_VERSION
from app.coerce import number
from app.errors import BusinessError

def closed(state):
    return state['archive']['status']=='可归档'

def plan(previous,event_ids,reason):
    if 'input_snapshot' not in previous: raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    if not isinstance(event_ids,list) or not event_ids or any(not isinstance(x,str) or not x.strip() for x in event_ids): raise BusinessError('BAD_EVENTS','撤回事件须为非空编号列表')
    if not isinstance(reason,str) or not reason.strip(): raise BusinessError('BAD_REASON','须填写撤回原因')
    events={r['id']:r for r in previous['records'] if r['kind']=='EVENT'}
    wanted={x.strip() for x in event_ids}
    missing=sorted(wanted-set(events))
    if missing: raise BusinessError('UNKNOWN_REFERENCE','撤回事件不存在: '+','.join(missing))
    prior=set(previous['input_snapshot']['context'].get('withdrawn_events',[]))
    if wanted<=prior: raise BusinessError('ALREADY_WITHDRAWN','事件已全部撤回，未产生新版本',409)
    return sorted(prior|wanted),sorted(wanted-prior)

def voucher(previous,successor,withdrawn_now,reason,operator):
    pred,succ=previous['manifest']['run_id'],successor['manifest']['run_id']
    before,after=number(previous['balance']['loss_kwh']),number(successor['balance']['loss_kwh'])
    events={r['id']:r for r in previous['records'] if r['kind']=='EVENT'}
    now=set(withdrawn_now)
    ranges=[(events[i]['device'],events[i]['start'],events[i]['end']) for i in sorted(now)]
    intervals=[]
    for seg in previous['energy_rows']:
        if not any(d==seg['device'] and s<seg['end'] and seg['start']<e for d,s,e in ranges): continue
        covered=any(u['device']==seg['device'] and u['start']<=seg['start'] and seg['end']<=u['end'] for u in successor['event_unions'])
        if covered: disp='仍被有效事件覆盖'
        elif seg['loss_kwh'] is None: disp='未定'
        else: disp='已恢复' if number(seg['loss_kwh'])==0 else '仍受限'
        loss=seg['loss_kwh']
        if loss is not None and number(loss)==0: loss='0'
        intervals.append({'device':seg['device'],'start':seg['start'],'end':seg['end'],'seconds':seg['seconds'],'loss_kwh':loss,'disposition':disp})
    kept=set(successor['input_snapshot']['context'].get('withdrawn_events',[]))
    effective=[{'id':r['id'],'device':r['device'],'start':r['start'],'end':r['end']} for r in successor['records'] if r['kind']=='EVENT' and r['id'] not in kept]
    vid=hashlib.sha256((pred+'|'+'|'.join(sorted(now))+'|'+RULE_VERSION).encode()).hexdigest()[:16]
    return {'voucher_id':vid,'predecessor_run_id':pred,'successor_run_id':succ,
            'withdrawn_event_ids':sorted(now),'reason':reason,'operator':operator,
            'withdrawn_rows':[{'id':i,'line':events[i].get('_line',0),'device':events[i]['device'],'start':events[i]['start'],'end':events[i]['end']} for i in sorted(now)],
            'loss_before_kwh':str(before),'loss_after_kwh':str(after),'delta_kwh':str(after-before),
            'intervals':intervals,'effective_event_ids':[e['id'] for e in effective],'effective_events':effective,
            'released':closed(successor),'evidence':{'sealed_export':f'/api/export/{pred}','successor_export':f'/api/export/{succ}'},
            'created_at':datetime.now(timezone.utc).isoformat()}
