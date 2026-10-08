#!/usr/bin/env python3
import argparse, json, os, time
import cv2
import numpy as np
from rknnlite.api import RKNNLite

OBJ_THRESH=0.25
NMS_THRESH=0.45
IMG_SIZE=(640,640)
CLASSES=("person","bicycle","car","motorbike","aeroplane","bus","train","truck","boat","traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat","dog","horse","sheep","cow","elephant","bear","zebra","giraffe","backpack","umbrella","handbag","tie","suitcase","frisbee","skis","snowboard","sports ball","kite","baseball bat","baseball glove","skateboard","surfboard","tennis racket","bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple","sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair","sofa","pottedplant","bed","diningtable","toilet","tvmonitor","laptop","mouse","remote","keyboard","cell phone","microwave","oven","toaster","sink","refrigerator","book","clock","vase","scissors","teddy bear","hair drier","toothbrush")

def letterbox(im, size=(640,640), color=(0,0,0)):
    h,w=im.shape[:2]; nh,nw=size
    r=min(nh/h,nw/w); rw,rh=int(round(w*r)),int(round(h*r))
    resized=cv2.resize(im,(rw,rh),interpolation=cv2.INTER_LINEAR)
    dw=(nw-rw)/2; dh=(nh-rh)/2
    left,right=int(round(dw-0.1)),int(round(dw+0.1)); top,bottom=int(round(dh-0.1)),int(round(dh+0.1))
    out=cv2.copyMakeBorder(resized,top,bottom,left,right,cv2.BORDER_CONSTANT,value=color)
    return out,r,dw,dh

def dfl(position):
    n,c,h,w=position.shape; mc=c//4
    x=position.reshape(n,4,mc,h,w)
    x=x-np.max(x,axis=2,keepdims=True); x=np.exp(x); x=x/np.sum(x,axis=2,keepdims=True)
    return np.sum(x*np.arange(mc,dtype=np.float32).reshape(1,1,mc,1,1),axis=2)

def box_process(position):
    gh,gw=position.shape[2:4]
    col,row=np.meshgrid(np.arange(gw),np.arange(gh)); grid=np.concatenate((col.reshape(1,1,gh,gw),row.reshape(1,1,gh,gw)),axis=1)
    stride=np.array([IMG_SIZE[1]//gh,IMG_SIZE[0]//gw]).reshape(1,2,1,1)
    p=dfl(position); xy1=(grid+0.5-p[:,:2])*stride; xy2=(grid+0.5+p[:,2:])*stride
    return np.concatenate((xy1,xy2),axis=1)

def flatten(x):
    return x.transpose(0,2,3,1).reshape(-1,x.shape[1])

def nms(boxes,scores):
    x1,y1,x2,y2=boxes[:,0],boxes[:,1],boxes[:,2],boxes[:,3]; areas=np.maximum(0,x2-x1)*np.maximum(0,y2-y1); order=scores.argsort()[::-1]; keep=[]
    while order.size:
        i=order[0]; keep.append(i)
        xx1=np.maximum(x1[i],x1[order[1:]]); yy1=np.maximum(y1[i],y1[order[1:]]); xx2=np.minimum(x2[i],x2[order[1:]]); yy2=np.minimum(y2[i],y2[order[1:]])
        inter=np.maximum(0,xx2-xx1)*np.maximum(0,yy2-yy1); iou=inter/(areas[i]+areas[order[1:]]-inter+1e-9)
        order=order[np.where(iou<=NMS_THRESH)[0]+1]
    return np.array(keep,dtype=np.int32)

def post_process_single(pred):
    """单输出布局 (1, 4+nc, 8400) → (boxes, classes, scores)。

    ultralytics 直接 export 出来的 ONNX/RKNN 就是这一种：前 4 通道**已经是
    解码好的 xyxy**（DFL 烧进图里了），后面 nc 通道是已 sigmoid 的类别分数，
    所以不需要 dfl()/box_process()。
    boxes 仍在 640×640 letterbox 坐标系，调用方的 (x-dw)/r 反变换照样适用。
    """
    p=np.asarray(pred)[0].T                       # (8400, 4+nc)
    # ！！前 4 通道是 **cxcywh**，不是 xyxy。实测 raw=[124.8,417.2,57.8,59.7]，
    # 后两位是宽高：按 cxcywh 画框恰好套住橙子，按 xyxy 画则飞出画面。
    # 调用方 fruit_service.py 是按 xyxy 做反 letterbox 的（[:,[0,2]] 当 x、
    # [:,[1,3]] 当 y），nms() 也按 xyxy 算 IoU，所以必须先转过来。
    xy=p[:,:4].astype(np.float32)
    boxes=np.stack([xy[:,0]-xy[:,2]*0.5, xy[:,1]-xy[:,3]*0.5,
                    xy[:,0]+xy[:,2]*0.5, xy[:,1]+xy[:,3]*0.5],axis=1)
    scores_all=p[:,4:]
    classes=np.argmax(scores_all,axis=1); scores=np.max(scores_all,axis=1)
    idx=np.where(scores>=OBJ_THRESH)[0]
    boxes,classes,scores=boxes[idx],classes[idx],scores[idx]
    if len(boxes)==0:return None,None,None
    ob=[]; oc=[]; os_=[]
    for c in set(classes.tolist()):
        ii=np.where(classes==c)[0]; k=nms(boxes[ii],scores[ii]); ob.append(boxes[ii][k]); oc.append(classes[ii][k]); os_.append(scores[ii][k])
    return np.concatenate(ob),np.concatenate(oc),np.concatenate(os_)

def post_process(outputs):
    # RKNN 出来可能是两种布局，这里都认：
    #
    #   9 输出 —— RKNN 官方 model_zoo 的 YOLO 布局，每尺度三个张量
    #     (box_dfl, class_scores, score_sum)，形状如
    #     [(1,64,80,80),(1,80,80,80),(1,1,80,80), ...40×3, ...20×3]。
    #     前 64 通道是 DFL 原始 logits，要现场解码。
    #     板上的 yolo11n_i8.rknn / yolo11n_fp.rknn 就是这种（80 = COCO 类数）。
    #
    #   1 输出 —— ultralytics 直接 export 的单张量 (1, 4+nc, 8400)。
    #     水果模型 fruit8_yolo11n_i8.rknn 是这种（12 = 4 box + 8 类）。
    #
    # 注意：**RKNN 不会自动把单输出切成 9 输出**。build_fruit_rknn.py 的注释
    # 曾以为会，那个假设只在 model_zoo 导出的 ONNX 上成立。单输出若喂进下面
    # 原来的循环，会死在 box_process 的 gh,gw=position.shape[2:4] 上
    # （(1,12,8400)[2:4] 只有一个元素），报
    #     not enough values to unpack (expected 2, got 1)
    if len(outputs)==1:return post_process_single(outputs[0])
    boxes=[]; probs=[]; pair=len(outputs)//3
    for i in range(3): boxes.append(flatten(box_process(outputs[pair*i]))); probs.append(flatten(outputs[pair*i+1]))
    boxes=np.concatenate(boxes); probs=np.concatenate(probs); classes=np.argmax(probs,axis=1); scores=np.max(probs,axis=1); idx=np.where(scores>=OBJ_THRESH)[0]
    boxes,classes,scores=boxes[idx],classes[idx],scores[idx]
    if len(boxes)==0:return None,None,None
    ob=[]; oc=[]; os=[]
    for c in set(classes.tolist()):
        ii=np.where(classes==c)[0]; k=nms(boxes[ii],scores[ii]); ob.append(boxes[ii][k]); oc.append(classes[ii][k]); os.append(scores[ii][k])
    return np.concatenate(ob),np.concatenate(oc),np.concatenate(os)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--model',default='/home/linaro/ai/models/yolo11n_fp.rknn'); ap.add_argument('--image'); ap.add_argument('--camera',default='/dev/video9'); ap.add_argument('--output',default='/home/linaro/ai/yolo11/out.jpg'); ap.add_argument('--json',default='/home/linaro/ai/yolo11/result.json'); a=ap.parse_args()
    if a.image: img=cv2.imread(a.image)
    else:
        cap=cv2.VideoCapture(a.camera); cap.set(cv2.CAP_PROP_FRAME_WIDTH,640); cap.set(cv2.CAP_PROP_FRAME_HEIGHT,480); ok,img=cap.read(); cap.release()
        if not ok: raise RuntimeError('camera capture failed: '+a.camera)
    if img is None: raise RuntimeError('image read failed')
    inp,r,dw,dh=letterbox(img); inp=cv2.cvtColor(inp,cv2.COLOR_BGR2RGB)
    rk=RKNNLite(); assert rk.load_rknn(a.model)==0; assert rk.init_runtime()==0
    t=time.time(); outputs=rk.inference(inputs=[np.expand_dims(inp,0)], data_format=['nhwc']); ms=(time.time()-t)*1000; rk.release()
    boxes,classes,scores=post_process(outputs); result=[]
    if boxes is not None:
        boxes[:,[0,2]]=(boxes[:,[0,2]]-dw)/r; boxes[:,[1,3]]=(boxes[:,[1,3]]-dh)/r
        boxes[:,[0,2]]=np.clip(boxes[:,[0,2]],0,img.shape[1]-1); boxes[:,[1,3]]=np.clip(boxes[:,[1,3]],0,img.shape[0]-1)
        for b,c,s in zip(boxes,classes,scores):
            x1,y1,x2,y2=[int(v) for v in b]; name=CLASSES[int(c)] if int(c)<len(CLASSES) else str(int(c)); result.append({'class':name,'confidence':float(s),'box':[x1,y1,x2,y2]}); cv2.rectangle(img,(x1,y1),(x2,y2),(255,0,0),2); cv2.putText(img,'%s %.2f'%(name,s),(x1,max(18,y1-5)),cv2.FONT_HERSHEY_SIMPLEX,0.5,(0,0,255),1)
    os.makedirs(os.path.dirname(a.output),exist_ok=True); cv2.imwrite(a.output,img)
    payload={'ok':True,'inference_ms':ms,'detections':result,'output':a.output}; open(a.json,'w').write(json.dumps(payload,ensure_ascii=False,indent=2)); print(json.dumps(payload,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
