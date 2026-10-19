"""Canonical read-only Qt performance evidence, without business writes or GPU overrides.

Run serially: python tools/web_performance_probe.py --output _tmp/agent_runs/performance/run --seconds 60
Optional --trace records Chromium events; do not compare traced and untraced runs.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import sqlite3
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil
import PySide6
from PySide6.QtCore import QTimer, QUrl, qVersion
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from frontend.web_bridge import LibraryBridge
from frontend.web_window import WebLibraryWindow, OfflinePage
import main


def run():
    parser=argparse.ArgumentParser(__doc__)
    from tools.agent_workspace import require_workspace
    parser.add_argument('--output',type=require_workspace,required=True)
    parser.add_argument('--seconds',type=int,default=60)
    parser.add_argument('--viewport',default='1728x900')
    parser.add_argument('--trace',action='store_true')
    parser.add_argument('--profile',action='store_true',help='Capture a Chromium CPU profile to attribute residual JavaScript tasks')
    parser.add_argument('--baseline-app',type=Path,help='Frozen pre-change app.js for same-entry A/B; never checks out the workspace')
    parser.add_argument('--record-lead-in',type=int,default=0,help='Diagnostic-only idle seconds for starting an external desktop recorder')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    # Open evidence streams before the application's read-only filesystem boundary.
    output=(args.output/'results.json').open('w',encoding='utf8')
    trace=gzip.open(args.output/'trace.jsonl.gz','wt',encoding='utf8') if args.trace else None
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    os.environ['QTWEBENGINE_REMOTE_DEBUGGING']=f'127.0.0.1:{port}'
    report={'mode':'canonical main.py --read-only','viewport':args.viewport,'seconds':args.seconds,
        'python':sys.version,'windows':platform.platform(),'qt':qVersion(),'pyside':PySide6.__version__,
        'console':[],'requests':[],'responses':[],'commands':[],'gui':[],'processes':[],'cdp':[],
        'trace_events':{},'metrics':[]}
    db=Path('data/mod_manager.db')
    before=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
    backup=sqlite3.connect(args.output/'before.db');before.backup(backup);backup.close();before.close()
    report['sources']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('frontend').rglob('*') if p.suffix in ('.py','.js','.css','.html')}
    if args.baseline_app:
        frozen=args.baseline_app.read_text('utf8');original_read=Path.read_text
        target=(Path('frontend/web/app.js')).resolve()
        def read_frozen(path,*a,**kw):return frozen if path==target else original_read(path,*a,**kw)
        Path.read_text=read_frozen
        report['baseline_app']=str(args.baseline_app)
        report['sources'][str(Path('frontend/web/app.js'))]=hashlib.sha256(args.baseline_app.read_bytes()).hexdigest()
    original_request=LibraryBridge.request;original_ack=LibraryBridge.acknowledge;original_execute=LibraryBridge.execute;original_init=WebLibraryWindow.__init__
    def request(self,rid,op,arguments):
        t=time.perf_counter();row={'id':rid,'op':op,'bytes':len(arguments.encode()),'start':t};report['requests'].append(row)
        original_request(self,rid,op,arguments);row['gui_ms']=(time.perf_counter()-t)*1000
    def execute(self,op,arguments):
        t=time.perf_counter();row={'op':op,'start':t};report.setdefault('worker',[]).append(row)
        try:return original_execute(self,op,arguments)
        finally:row['ms']=(time.perf_counter()-t)*1000
    def acknowledge(self,rid):
        t=time.perf_counter()
        try:return original_ack(self,rid)
        finally:report.setdefault('acknowledgements',[]).append({'id':rid,'start':t,'ms':(time.perf_counter()-t)*1000})
    def command(self,*a):report['commands'].append(str(a));raise AssertionError('Production write refused')
    LibraryBridge.request=request;LibraryBridge.acknowledge=acknowledge;LibraryBridge.execute=execute;LibraryBridge.execute_command=command
    OfflinePage.javaScriptConsoleMessage=lambda self,level,message,line,source:report['console'].append({'level':str(level),'message':message,'line':line})
    def init(w,*a,**kw):
        original_init(w,*a,**kw);w.resize(*map(int,args.viewport.split('x')))
        w.page.renderProcessTerminated.connect(lambda status,code:report.setdefault('renderer_terminations',[]).append({'status':str(status),'code':code}))
        w.bridge.response.connect(lambda message:report['responses'].append({'time':time.perf_counter(),'bytes':len(message.encode()),'id':json.loads(message)['request']}))
        QTimer.singleShot(800,lambda:start(w))
    WebLibraryWindow.__init__=init
    def start(w):
        w.resize(*map(int,args.viewport.split('x')));w.move(0,0);w.raise_();w.activateWindow()
        report['screen']={'dpr':w.devicePixelRatioF(),'refresh':w.screen().refreshRate(),'geometry':w.screen().geometry().getRect()}
        report['window_rect']=w.frameGeometry().getRect()
        print('capture rectangle:',report['window_rect'],'DPR',w.devicePixelRatioF(),flush=True)
        report['pid']=os.getpid();report['window_title']=w.windowTitle()
        timers=[];state={'stage':'startup','polling':False,'last':time.perf_counter(),'id':0,'closing':False}
        ws=QWebSocket(parent=w);w._perf_ws=ws
        def send(method,params=None):
            state['id']+=1;ws.sendTextMessage(json.dumps({'id':state['id'],'method':method,'params':params or {}}))
            return state['id']
        def message(raw):
            d=json.loads(raw)
            if d.get('method')=='Tracing.dataCollected':
                for event in d['params']['value']:
                    key=event.get('name','');summary=report['trace_events'].setdefault(key,{'count':0,'duration_us':0});summary['count']+=1;summary['duration_us']+=event.get('dur',0)
                    if trace:trace.write(json.dumps(event)+'\n')
            elif d.get('method')=='Tracing.tracingComplete':finish_close(w)
            elif d.get('id')==state.get('profile_stop') and 'result' in d:
                report['cpu_profile']=d['result'].get('profile');finish_recording()
            elif 'result' in d and 'metrics' in d['result']:
                report['metrics'].append({'stage':state['stage'],'time':time.perf_counter(),'values':{m['name']:m['value'] for m in d['result']['metrics']}})
            else:report['cdp'].append(d)
        ws.textMessageReceived.connect(message)
        # Query the local browser target for actual compositor/GPU diagnostics.
        manager=QNetworkAccessManager(w);w._perf_network=manager
        gpu_socket=QWebSocket(parent=w);w._perf_gpu_socket=gpu_socket
        gpu_socket.connected.connect(lambda:gpu_socket.sendTextMessage(json.dumps({'id':1,'method':'SystemInfo.getInfo'})))
        gpu_socket.textMessageReceived.connect(lambda raw:report.update(gpu=json.loads(raw)))
        reply=manager.get(QNetworkRequest(QUrl(f'http://127.0.0.1:{port}/json/version')))
        def gpu_target():
            try:gpu_socket.open(QUrl(json.loads(bytes(reply.readAll()))['webSocketDebuggerUrl']))
            except (ValueError,KeyError) as error:report['gpu_error']=str(error)
            reply.deleteLater()
        reply.finished.connect(gpu_target)
        def connected():
            send('Browser.getVersion');send('Performance.enable')
            if trace:send('Tracing.start',{'categories':'devtools.timeline,disabled-by-default-devtools.timeline','options':'record-continuously'})
            if args.profile:
                send('Profiler.enable');send('Profiler.setSamplingInterval',{'interval':1000});send('Profiler.start')
        ws.connected.connect(connected)
        ws.open(QUrl(f'ws://127.0.0.1:{port}/devtools/page/{w.page.devToolsId()}'))
        def heartbeat():
            now=time.perf_counter();report['gui'].append([state['stage'],(now-state['last'])*1000]);state['last']=now
        timer=QTimer(w);timer.timeout.connect(heartbeat);timer.start(20);timers.append(timer)
        stopped=threading.Event();gui_thread=threading.get_ident()
        def watchdog():
            while not stopped.wait(.025):
                lag=(time.perf_counter()-state['last'])*1000
                if lag<60:continue
                frame=sys._current_frames().get(gui_thread);stack=[]
                while frame and len(stack)<10:
                    stack.append([frame.f_code.co_filename,frame.f_code.co_name,frame.f_lineno]);frame=frame.f_back
                report.setdefault('gui_watchdog',[]).append({'stage':state['stage'],'gap_ms':lag,'stack':stack})
        threading.Thread(target=watchdog,daemon=True).start()
        processes={}
        def sample():
            started=time.perf_counter()
            entries=[]
            for proc in [psutil.Process(),*psutil.Process().children(recursive=True)]:
                try:
                    if proc.pid not in processes:processes[proc.pid]=proc;proc.cpu_percent()
                    p=processes[proc.pid];mem=p.memory_info();entries.append({'pid':p.pid,'name':p.name(),'cpu_percent':p.cpu_percent(),'rss':mem.rss,'private':getattr(mem,'private',None),'cmd':p.cmdline()})
                except (psutil.NoSuchProcess,psutil.AccessDenied):continue
            report['processes'].append({'time':time.perf_counter(),'stage':state['stage'],'entries':entries})
            report.setdefault('sampler_ms',[]).append((time.perf_counter()-started)*1000)
        def monitor():
            while not stopped.wait(1):sample()
        threading.Thread(target=monitor,daemon=True).start()
        def finish_close(window):
            if state['closing']:return
            state['closing']=True
            stopped.set()
            for t in timers:t.stop()
            window.close()
        def finish_recording():
            if trace:send('Tracing.end');QTimer.singleShot(4000,lambda:finish_close(w))
            else:finish_close(w)
        def data_done(raw):
            report['javascript']=json.loads(raw)
            if args.profile:
                state['profile_stop']=send('Profiler.stop');QTimer.singleShot(4000,lambda:finish_close(w))
            else:finish_recording()
        def status_done(raw):
            state['polling']=False
            if not raw:return
            info=json.loads(raw)
            if info['stage']!=state['stage']:
                state['stage']=info['stage'];send('Performance.getMetrics');print('stage:',state['stage'],flush=True)
            if info.get('window'):
                w.page.runJavaScript('perfWindowRequest=null')
                w.resize(1180,760);QTimer.singleShot(300,w.showMaximized);QTimer.singleShot(600,w.showNormal)
                def restored():
                    w.resize(*map(int,args.viewport.split('x')));w.page.runJavaScript('perfWindowRequest=false')
                QTimer.singleShot(1000,restored)
            if info['stage'] in ('finished','failed'):
                poll.stop();send('Performance.getMetrics');w.page.runJavaScript('JSON.stringify(perf)',data_done)
        def poll_status():
            if state['polling']:return
            state['polling']=True;w.page.runJavaScript("typeof perf!=='undefined'?JSON.stringify({stage:perf.stage,window:globalThis.perfWindowRequest}):null",status_done)
        poll=QTimer(w);poll.timeout.connect(poll_status);poll.start(150);timers.append(poll)
        code=Path(__file__).with_name('web_performance_scenario.js').read_text('utf8')
        w.page.runJavaScript(code)
        QTimer.singleShot(args.record_lead_in*1000,lambda:w.page.runJavaScript('startPerformance('+str(args.seconds)+');'))
        QTimer.singleShot(max(args.seconds+180,300)*1000,lambda:finish_close(w))
    main._notify_already_running=lambda:report.update(error='Existing SMM instance; close normally')
    report['exit']=main.main(['--read-only'])
    if trace:trace.close()
    json.dump(report,output,ensure_ascii=False);output.close()
    print(json.dumps({'finished':report.get('javascript',{}).get('stage'),'errors':report.get('javascript',{}).get('errors'),'commands':report['commands'],'output':str(args.output)},ensure_ascii=False))
    return 0 if report.get('javascript',{}).get('stage')=='finished' else 1


if __name__=='__main__':raise SystemExit(run())
