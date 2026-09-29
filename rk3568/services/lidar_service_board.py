#!/usr/bin/env python3
import json, math, os, signal, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import serial

PORT='/dev/lidar'; BAUD=150000; HTTP_PORT=8091
STOP=False
LOCK=threading.Lock()
STATE={'ok':False,'connected':False,'status':'waiting_device','port':PORT,'baudrate':BAUD,'present':False,'points':0,'scan_hz':0.0,'nearest_m':None,'front_m':None,'left_m':None,'right_m':None,'packets':0,'frames':0,'protocol_layout':'unknown','last_update':0}
SCAN=[]
FRAME_TIMES=[]

def update(**kw):
    with LOCK: STATE.update(kw)
def snapshot():
    with LOCK: return dict(STATE), list(SCAN)

def corrected_angle(raw_angle_deg, distance_mm):
    if distance_mm <= 0: return raw_angle_deg % 360.0
    return (raw_angle_deg + math.degrees(math.atan(21.8*(155.3-distance_mm)/(155.3*distance_mm)))) % 360.0

def parse_packet(pkt, has_cs):
    lsn=pkt[3]
    if lsn < 1: return []
    fsa=pkt[4] | (pkt[5]<<8); lsa=pkt[6] | (pkt[7]<<8)
    a0=(fsa>>1)/64.0; a1=(lsa>>1)/64.0; diff=(a1-a0)%360.0
    base=10 if has_cs else 8; pts=[]
    for i in range(lsn):
        off=base+3*i
        if off+2 >= len(pkt): break
        raw=pkt[off] | (pkt[off+1]<<8); dist_mm=raw/4.0; quality=pkt[off+2]
        angle=a0 if lsn==1 else a0+diff*i/(lsn-1)
        angle=corrected_angle(angle,dist_mm)
        if 80 <= dist_mm <= 16000: pts.append((angle,dist_mm/1000.0,quality))
    return pts

def sector_min(points, center, half=35):
    vals=[]
    for a,d,q in points:
        delta=(a-center+180)%360-180
        if abs(delta)<=half: vals.append(d)
    return min(vals) if vals else None

def publish_scan(points, started):
    global SCAN
    if len(points)<20: return started
    now=time.time()
    global FRAME_TIMES
    FRAME_TIMES.append(now)
    FRAME_TIMES=FRAME_TIMES[-8:]
    hz=0.0 if len(FRAME_TIMES)<2 else (len(FRAME_TIMES)-1)/max(FRAME_TIMES[-1]-FRAME_TIMES[0],1e-6)
    usable=[(a,d,q) for a,d,q in points if d >= 0.12]
    nearest=min((d for _,d,_ in usable),default=None); front=sector_min(usable,0,45); left=sector_min(usable,90,45); right=sector_min(usable,270,45)
    # Presence is deliberately conservative: valid return within 3m in any sector.
    present=nearest is not None and nearest <= 3.0
    scan=[{'angle_deg':round(a,2),'distance_m':round(d,3),'quality':int(q)} for a,d,q in points]
    with LOCK:
        SCAN=scan
        STATE.update({'ok':True,'connected':True,'status':'running','present':present,'points':len(scan),'scan_hz':round(hz,2),'nearest_m':None if nearest is None else round(nearest,3),'front_m':None if front is None else round(front,3),'left_m':None if left is None else round(left,3),'right_m':None if right is None else round(right,3),'frames':STATE['frames']+1,'last_update':int(now),'error':''})
    return now

def serial_loop():
    buf=bytearray(); circle=[]; circle_start=0.0; ser=None
    while not STOP:
        if ser is None:
            if not os.path.exists(PORT): update(ok=False,connected=False,status='waiting_device',present=False); time.sleep(0.5); continue
            try:
                ser=serial.Serial(PORT,BAUD,timeout=0.1,write_timeout=0.5,exclusive=True)
                ser.reset_input_buffer(); ser.write(bytes([0xA5,0x60])); ser.flush(); update(connected=True,status='starting_scan',port=os.path.realpath(PORT)); buf.clear(); circle=[]; circle_start=time.time()
            except Exception as e: update(ok=False,connected=False,status='open_error',error=str(e)); ser=None; time.sleep(1); continue
        try:
            data=ser.read(4096)
            if data: buf.extend(data)
            elif time.time()-circle_start>3: update(status='no_data',present=False)
            while True:
                pos=buf.find(b'\xAA\x55')
                if pos<0:
                    if len(buf)>1: del buf[:-1]
                    break
                if pos: del buf[:pos]
                if len(buf)<4: break
                lsn=buf[3]
                if lsn==0 or lsn>120: del buf[0]; continue
                n0=8+3*lsn; n1=10+3*lsn; layout=None; plen=None
                if len(buf)>=n0+2 and buf[n0:n0+2]==b'\xAA\x55': layout='no_checksum'; plen=n0
                elif len(buf)>=n1+2 and buf[n1:n1+2]==b'\xAA\x55': layout='checksum_2b'; plen=n1
                elif len(buf)<n1+2: break
                else:
                    # Last/isolated packet fallback follows the buyer-tested driver layout.
                    layout='no_checksum'; plen=n0
                pkt=bytes(buf[:plen]); del buf[:plen]
                ct=pkt[2]; pts=parse_packet(pkt,layout=='checksum_2b')
                with LOCK: STATE['packets']+=1; STATE['protocol_layout']=layout
                if ct&1 and circle:
                    circle_start=publish_scan(circle,circle_start); circle=[]
                circle.extend(pts)
                # Angle wrap fallback if CT start flag is absent.
                if len(circle)>2 and circle[-1][0] < circle[-2][0]-180:
                    prev=circle[:-1]; circle=[circle[-1]]; circle_start=publish_scan(prev,circle_start)
        except Exception as e:
            update(ok=False,connected=False,status='read_error',error=str(e),present=False)
            try: ser.close()
            except Exception: pass
            ser=None; time.sleep(0.5)
    if ser:
        try: ser.write(bytes([0xA5,0x00,0xA5,0x65,0xA5,0x65])); ser.close()
        except Exception: pass

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_): return
    def send_json(self,obj):
        b=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        state,scan=snapshot()
        if self.path.startswith('/api/radar/scan'): self.send_json({'state':state,'scan':scan})
        elif self.path.startswith('/api/radar/status') or self.path.startswith('/health'): self.send_json(state)
        else:
            html='''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RK3568 YDLIDAR</title><style>body{background:#111;color:#eee;font-family:Arial;margin:0;padding:16px;max-width:980px;margin:auto}.top{display:flex;justify-content:space-between;align-items:center}.ok{color:#1fce72}.bad{color:#ff6b6b}.grid{display:grid;grid-template-columns:minmax(420px,700px) 260px;gap:16px;align-items:start}canvas{background:#020608;width:100%;border:1px solid #173;border-radius:6px}.card{background:#222;padding:12px;margin-top:12px;border-radius:8px}pre{white-space:pre-wrap;font-size:12px}.metric{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #333}.hint{color:#9aa;font-size:12px;line-height:1.5}@media(max-width:760px){.grid{display:block}}</style><div class="top"><h2>YDLIDAR S2-YJ</h2><b id="status">连接中</b></div><div class="grid"><canvas id="c" width="700" height="700"></canvas><div class="card"><div class="metric"><span>扫描频率</span><b id="hz">-</b></div><div class="metric"><span>点数</span><b id="pts">-</b></div><div class="metric"><span>最近点</span><b id="near">-</b></div><div class="metric"><span>前方</span><b id="front">-</b></div><div class="metric"><span>左侧</span><b id="left">-</b></div><div class="metric"><span>右侧</span><b id="right">-</b></div><div class="metric"><span>协议</span><b id="proto">-</b></div><p class="hint">圆环每格1米，视图显示半径4米；绿色/黄色/红色表示回波质量从低到高。原始点云接口仍保留。</p><pre id="raw"></pre></div></div><script>const c=document.getElementById('c'),x=c.getContext('2d');function fmt(v){return v==null?'—':Number(v).toFixed(2)+' m'}function draw(d){let w=c.width,h=c.height,k=w/2/4;x.fillStyle='#020608';x.fillRect(0,0,w,h);x.strokeStyle='#164';x.lineWidth=1;for(let r=1;r<=4;r++){x.beginPath();x.arc(w/2,h/2,r*k,0,Math.PI*2);x.stroke();x.fillStyle='#496';x.font='12px Arial';x.fillText(r+'m',w/2+4,h/2-r*k+14)}x.strokeStyle='#163';for(let a=0;a<360;a+=45){let t=a*Math.PI/180;x.beginPath();x.moveTo(w/2,h/2);x.lineTo(w/2+Math.sin(t)*4*k,h/2-Math.cos(t)*4*k);x.stroke()}for(let p of d.scan){if(p.distance_m>4)continue;let t=p.angle_deg*Math.PI/180;let q=p.quality;x.fillStyle=q<25?'#24d878':q<70?'#ffd43b':'#ff5c5c';x.fillRect(w/2+Math.sin(t)*p.distance_m*k-1,h/2-Math.cos(t)*p.distance_m*k-1,3,3)}}async function u(){try{let d=await(await fetch('/api/radar/scan?t='+Date.now())).json(),s=d.state;status.textContent=s.connected?'在线':'等待雷达';status.className=s.connected?'ok':'bad';hz.textContent=(s.scan_hz||0).toFixed(1)+' Hz';pts.textContent=s.points||0;near.textContent=fmt(s.nearest_m);front.textContent=fmt(s.front_m);left.textContent=fmt(s.left_m);right.textContent=fmt(s.right_m);proto.textContent=s.protocol_layout||'-';raw.textContent=JSON.stringify(s,null,2);draw(d)}catch(e){status.textContent='连接失败';status.className='bad'}setTimeout(u,120)}u()</script>'''.encode('utf-8')
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(html))); self.end_headers(); self.wfile.write(html)

def sig(*_):
    global STOP; STOP=True
signal.signal(signal.SIGTERM,sig); signal.signal(signal.SIGINT,sig)
threading.Thread(target=serial_loop,daemon=True).start()
server=ThreadingHTTPServer(('0.0.0.0',HTTP_PORT),Handler)
try: server.serve_forever()
finally: STOP=True; server.server_close()
