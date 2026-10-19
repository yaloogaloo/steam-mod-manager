"""Summarize raw canonical Qt probe results; keep frame tails and work counts separate."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics


def distribution(values):
    values=sorted(values)
    if not values:return {'n':0}
    return {'n':len(values),'median':statistics.median(values),'p95':values[int((len(values)-1)*.95)],
            'p99':values[int((len(values)-1)*.99)],'max':values[-1],
            'over50':sum(v>50 for v in values),'over100':sum(v>100 for v in values)}


def summarize(path):
    d=json.loads(path.read_text('utf8'));j=d.get('javascript',{})
    result={'path':str(path),'viewport':d['viewport'],'screen':d.get('screen'),
        'stage':j.get('stage'),'cycles':j.get('cycles'),'elapsed_ms':j.get('elapsed'),
        'console':d['console'],'errors':j.get('errors'),'commands':d['commands'],'stages':{}}
    for name in sorted({r[0] for r in j.get('frames',[])}):
        calls=[r for r in j['calls'] if r[0]==name];mutations=[r for r in j['mutations'] if r[0]==name]
        result['stages'][name]={'frames_ms':distribution([r[1] for r in j['frames'] if r[0]==name]),
            'gui_intervals_ms':distribution([r[1] for r in d['gui'] if r[0]==name]),
            'gui_delay_ms':distribution([max(0,r[1]-20) for r in d['gui'] if r[0]==name]),
            'calls':dict(Counter(r[1] for r in calls)), 'js_sync_ms':sum(r[2] for r in calls),
            'dom_added':sum(r[1] for r in mutations),'dom_removed':sum(r[2] for r in mutations),
            'cards_added':sum(r[3] for r in mutations),'cards_removed':sum(r[4] for r in mutations),
            'long_tasks_ms':distribution([r[2] for r in j['longTasks'] if r[0]==name]),
            'input_complete_ms':distribution([r[1] for r in j['inputs'] if r[0]==name]),
            'rpc_count':dict(Counter(r[1] for r in j['rpc'] if r[0]==name)),
            'image_load_events':sum(r[0]==name for r in j['images'])}
    result['bridge']={'requests':len(d['requests']),'bytes_out':sum(r['bytes'] for r in d['responses']),
        'bytes_in':sum(r['bytes'] for r in d['requests']),
        'gui_slot_ms':distribution([r['gui_ms'] for r in d['requests']]),'worker':{}}
    result['bridge']['acknowledgement_ms']=distribution([r['ms'] for r in d.get('acknowledgements',[])])
    for name in {r['op'] for r in d.get('worker',[])}:
        result['bridge']['worker'][name]=distribution([r['ms'] for r in d['worker'] if r['op']==name])
    values=d.get('metrics',[])
    if len(values)>1:
        a,b=values[0]['values'],values[-1]['values']
        result['chromium_delta']={k:b[k]-a[k] for k in ['LayoutCount','RecalcStyleCount','LayoutDuration','RecalcStyleDuration','ScriptDuration','TaskDuration']}
        result['heap_first_last']=[a['JSHeapUsedSize'],b['JSHeapUsedSize']]
    result['processes']={}
    for name in ['python.exe','QtWebEngineProcess.exe']:
        totals=[];cpus=[]
        for sample in d['processes']:
            entries=[e for e in sample['entries'] if e['name']==name]
            if entries:totals.append(sum(e['rss'] for e in entries));cpus.append(sum(e['cpu_percent'] for e in entries))
        if totals:result['processes'][name]={'rss_first':totals[0],'rss_last':totals[-1],'rss_peak':max(totals),'cpu_percent_median':statistics.median(cpus),'cpu_percent_peak':max(cpus)}
    result['trace_events']=d.get('trace_events',{})
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(__doc__);p.add_argument('results',type=Path);p.add_argument('--output',type=Path)
    args=p.parse_args();value=summarize(args.results);text=json.dumps(value,ensure_ascii=False,indent=2)
    if args.output:args.output.write_text(text,encoding='utf8')
    else:print(text)
