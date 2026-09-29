#!/usr/bin/env python3
import argparse, glob, json, os, signal, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import cv2
import numpy as np
from rknnlite.api import RKNNLite
from yolo11_infer import letterbox, nms, OBJ_THRESH, CLASSES


def post_process_fast(outputs):
    """Use YOLO11's score-sum heads to skip background DFL/class work."""
    branch_boxes=[]; branch_classes=[]; branch_scores=[]
    for branch in range(3):
        box_tensor=outputs[branch*3][0]
        score_tensor=outputs[branch*3+1][0]
        score_sum=outputs[branch*3+2][0,0].reshape(-1)
        candidate=np.flatnonzero(score_sum>=OBJ_THRESH)
        if candidate.size==0: continue
        channels,grid_h,grid_w=box_tensor.shape
        class_values=score_tensor.reshape(score_tensor.shape[0],-1)[:,candidate].T
        classes=np.argmax(class_values,axis=1)
        scores=class_values[np.arange(candidate.size),classes]
        keep=scores>=OBJ_THRESH
        if not np.any(keep): continue
        candidate=candidate[keep]; classes=classes[keep]; scores=scores[keep]
        dfl_len=channels//4
        logits=box_tensor.reshape(channels,-1)[:,candidate].T.reshape(-1,4,dfl_len)
        logits=logits-np.max(logits,axis=2,keepdims=True)
        weights=np.exp(logits); weights/=np.sum(weights,axis=2,keepdims=True)
        distance=np.sum(weights*np.arange(dfl_len,dtype=np.float32).reshape(1,1,-1),axis=2)
        row=(candidate//grid_w).astype(np.float32); col=(candidate%grid_w).astype(np.float32)
        stride=float(640//grid_h)
        boxes=np.stack(((col+.5-distance[:,0])*stride,
                        (row+.5-distance[:,1])*stride,
                        (col+.5+distance[:,2])*stride,
                        (row+.5+distance[:,3])*stride),axis=1)
        branch_boxes.append(boxes); branch_classes.append(classes); branch_scores.append(scores)
    if not branch_boxes: return None,None,None
    boxes=np.concatenate(branch_boxes); classes=np.concatenate(branch_classes); scores=np.concatenate(branch_scores)
    out_boxes=[]; out_classes=[]; out_scores=[]
    for class_id in np.unique(classes):
        indexes=np.flatnonzero(classes==class_id); kept=nms(boxes[indexes],scores[indexes])
        out_boxes.append(boxes[indexes][kept]); out_classes.append(classes[indexes][kept]); out_scores.append(scores[indexes][kept])
    return np.concatenate(out_boxes),np.concatenate(out_classes),np.concatenate(out_scores)

class VisionState:
    def __init__(self):
        self.lock=threading.Lock()
        self.result={'ok':False,'status':'starting','detections':[]}
        self.jpeg=b''
        self.raw_frame=None
    def update(self,result,jpeg,raw_frame=None):
        with self.lock: self.result=result; self.jpeg=jpeg; self.raw_frame=raw_frame
    def get(self):
        with self.lock: return dict(self.result), bytes(self.jpeg), self.raw_frame

STATE=VisionState()
STOP=False
DASH=b'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RK3568 Vision</title><style>body{font-family:Arial;background:#111;color:#eee;max-width:900px;margin:auto;padding:20px}img{width:100%;border:1px solid #555}.card{background:#222;padding:14px;margin-top:12px;border-radius:8px}pre{white-space:pre-wrap}</style><h2>ATK-DLRK3568 YOLO11 INT8 NPU</h2><img id="v" src="/stream.mjpg"><div class="card"><pre id="r">starting...</pre></div><script>async function u(){try{r.textContent=JSON.stringify(await(await fetch('/api/vision/result?t='+Date.now())).json(),null,2)}catch(e){r.textContent=e}setTimeout(u,250)}u()</script>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args): return
    def do_GET(self):
        result,jpeg,raw_frame=STATE.get()
        if self.path.startswith('/api/vision/result') or self.path.startswith('/health'):
            body=json.dumps(result,ensure_ascii=False).encode('utf-8'); self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        elif self.path.startswith('/snapshot.jpg'):
            if not jpeg: self.send_error(503,'No frame'); return
            self.send_response(200); self.send_header('Content-Type','image/jpeg'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(jpeg))); self.end_headers(); self.wfile.write(jpeg)
        elif self.path.startswith('/raw.jpg'):
            if raw_frame is None: self.send_error(503,'No frame'); return
            ok,raw_jpg=cv2.imencode('.jpg',raw_frame,[int(cv2.IMWRITE_JPEG_QUALITY),92])
            if not ok: self.send_error(500,'JPEG encode failed'); return
            raw_jpg=raw_jpg.tobytes(); self.send_response(200); self.send_header('Content-Type','image/jpeg'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(raw_jpg))); self.end_headers(); self.wfile.write(raw_jpg)
        elif self.path.startswith('/stream.mjpg'):
            self.send_response(200); self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame'); self.send_header('Cache-Control','no-store'); self.end_headers()
            last_seq=-1
            try:
                while not STOP:
                    result,jpeg,_raw_frame=STATE.get(); seq=result.get('sequence',-1)
                    if jpeg and seq!=last_seq:
                        self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(jpeg)).encode()+b'\r\n\r\n'+jpeg+b'\r\n'); self.wfile.flush(); last_seq=seq
                    time.sleep(0.03)
            except (BrokenPipeError,ConnectionResetError): pass
        else:
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(DASH))); self.end_headers(); self.wfile.write(DASH)

def sig(*_):
    global STOP; STOP=True

def find_camera(preferred):
    if preferred != 'auto' and os.path.exists(preferred): return preferred
    for dev in sorted(glob.glob('/dev/video*')):
        name=os.path.basename(dev)
        try:
            sysname=open('/sys/class/video4linux/%s/name'%name).read().strip().lower()
            idx=open('/sys/class/video4linux/%s/index'%name).read().strip()
        except Exception: continue
        if idx=='0' and ('usb' in sysname or 'camera' in sysname): return dev
    return None

class LatestCamera:
    def __init__(self, preferred):
        self.preferred=preferred; self.lock=threading.Lock(); self.frame=None; self.frame_time=0.0; self.seq=0; self.device=None; self.running=True
        self.thread=threading.Thread(target=self.loop,daemon=True); self.thread.start()
    def open(self):
        dev=find_camera(self.preferred)
        if not dev: return None
        cap=cv2.VideoCapture(dev); cap.set(cv2.CAP_PROP_FOURCC,cv2.VideoWriter_fourcc(*'MJPG')); cap.set(cv2.CAP_PROP_FRAME_WIDTH,640); cap.set(cv2.CAP_PROP_FRAME_HEIGHT,480); cap.set(cv2.CAP_PROP_BUFFERSIZE,1)
        if not cap.isOpened(): cap.release(); return None
        self.device=dev; return cap
    def loop(self):
        cap=None
        while self.running and not STOP:
            if cap is None:
                cap=self.open()
                if cap is None: time.sleep(1); continue
            ok,frame=cap.read()
            if not ok:
                cap.release(); cap=None; self.device=None
                with self.lock: self.frame=None
                time.sleep(0.5); continue
            with self.lock: self.frame=frame; self.frame_time=time.monotonic(); self.seq+=1
        if cap is not None: cap.release()
    def latest(self,last_seq):
        with self.lock:
            if self.frame is None or self.seq==last_seq: return None,self.seq,self.device,self.frame_time
            return self.frame.copy(),self.seq,self.device,self.frame_time
    def close(self): self.running=False; self.thread.join(timeout=2)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--model',default='/home/linaro/ai/models/yolo11n_i8.rknn'); ap.add_argument('--camera',default='auto'); ap.add_argument('--port',type=int,default=8088); ap.add_argument('--interval',type=float,default=0.0); a=ap.parse_args()
    signal.signal(signal.SIGTERM,sig); signal.signal(signal.SIGINT,sig)
    rk=RKNNLite()
    if rk.load_rknn(a.model)!=0 or rk.init_runtime()!=0: raise RuntimeError('RKNN init failed')
    # Warm the graph and allocator before publishing latency numbers.
    warm=np.zeros((1,640,640,3),dtype=np.uint8)
    for _ in range(3): rk.inference(inputs=[warm],data_format=['nhwc'])
    cam=LatestCamera(a.camera)
    http=ThreadingHTTPServer(('0.0.0.0',a.port),Handler); threading.Thread(target=http.serve_forever,daemon=True).start()
    seq=0; cam_seq=-1; last=time.monotonic(); fps=0.0
    try:
        while not STOP:
            iteration_start=time.monotonic()
            img,new_cam_seq,dev,capture_time=cam.latest(cam_seq)
            if img is None:
                STATE.update({'ok':False,'status':'waiting_camera','camera':dev or a.camera,'detections':[]},b'',None); time.sleep(0.1); continue
            cam_seq=new_cam_seq
            source_age_ms=(time.monotonic()-capture_time)*1000
            stage=time.monotonic()
            inp,r,dw,dh=letterbox(img); inp=cv2.cvtColor(inp,cv2.COLOR_BGR2RGB)
            preprocess_ms=(time.monotonic()-stage)*1000
            stage=time.monotonic(); outputs=rk.inference(inputs=[np.expand_dims(inp,0)],data_format=['nhwc']); infer_ms=(time.monotonic()-stage)*1000
            stage=time.monotonic()
            boxes,classes,scores=post_process_fast(outputs); detections=[]
            display=img.copy()
            if boxes is not None:
                boxes[:,[0,2]]=(boxes[:,[0,2]]-dw)/r; boxes[:,[1,3]]=(boxes[:,[1,3]]-dh)/r; boxes[:,[0,2]]=np.clip(boxes[:,[0,2]],0,img.shape[1]-1); boxes[:,[1,3]]=np.clip(boxes[:,[1,3]],0,img.shape[0]-1)
                for b,c,score in zip(boxes,classes,scores):
                    x1,y1,x2,y2=[int(v) for v in b]; name=CLASSES[int(c)] if int(c)<len(CLASSES) else str(int(c)); detections.append({'class':name,'confidence':round(float(score),4),'box':[x1,y1,x2,y2]}); cv2.rectangle(display,(x1,y1),(x2,y2),(255,0,0),2); cv2.putText(display,'%s %.2f'%(name,score),(x1,max(18,y1-5)),cv2.FONT_HERSHEY_SIMPLEX,0.5,(0,0,255),1)
            postprocess_ms=(time.monotonic()-stage)*1000
            now=time.monotonic(); inst=1/max(now-last,1e-6); last=now; fps=inst if seq==0 else 0.8*fps+0.2*inst; seq+=1
            cv2.putText(display,'RK3568 INT8 %.1f FPS infer %.0f ms age %.0f ms'%(fps,infer_ms,source_age_ms),(8,22),cv2.FONT_HERSHEY_SIMPLEX,0.48,(0,255,0),2)
            stage=time.monotonic()
            ok,jpg=cv2.imencode('.jpg',display,[int(cv2.IMWRITE_JPEG_QUALITY),72]); data=jpg.tobytes() if ok else b''
            encode_ms=(time.monotonic()-stage)*1000
            total_ms=(time.monotonic()-iteration_start)*1000
            result={'ok':True,'status':'running','sequence':seq,'timestamp_ms':int(time.time()*1000),'camera':dev,'model':os.path.basename(a.model),'source_frame_age_ms':round(source_age_ms,2),'preprocess_ms':round(preprocess_ms,2),'inference_ms':round(infer_ms,2),'postprocess_ms':round(postprocess_ms,2),'encode_ms':round(encode_ms,2),'pipeline_ms':round(total_ms,2),'estimated_display_age_ms':round(source_age_ms+total_ms,2),'loop_fps':round(fps,2),'detections':detections}
            STATE.update(result,data,img)
            if seq%10==0:
                tmp='/home/linaro/ai/yolo11/runtime/latest.json.tmp'; open(tmp,'w').write(json.dumps(result,ensure_ascii=False,indent=2)); os.replace(tmp,'/home/linaro/ai/yolo11/runtime/latest.json')
            if a.interval: time.sleep(a.interval)
    finally:
        http.shutdown(); cam.close(); rk.release()
if __name__=='__main__': main()
