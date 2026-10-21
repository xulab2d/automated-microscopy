"""Research-only inference on existing public masks and fixed raw-image previews."""
from pathlib import Path
import argparse,json,time
import cv2,numpy as np,torch
from pycocotools import mask as coco_mask
from probe_cpu import load_model,predict

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--count',type=int,default=10);parser.add_argument('--max-side',type=int,default=1536)
    parser.add_argument('--output',default='external/maskterial-hf/benchmark');parser.add_argument('--weights');args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True);torch.set_num_threads(8);model=load_model(args.weights)
    root=Path('external/benchmarks');data=json.loads((root/'RLE_annotations/test_annotations_300.json').read_text())
    chosen=json.loads(Path('data/public-positive-holdout.json').read_text())['results'][:args.count]
    names=[entry['image'] for entry in chosen];images={i['file_name']:i for i in data['images']}
    result={'checkpoint':args.weights or 'MaskTerial synthetic pretrained','split':'Real_MoSe2 test, existing ten-image holdout selection','score_threshold':.5,'min_native_area_px':667,'iou_threshold':.5,'results':[]}
    for name in names:
        info=images[name];bgr=cv2.imread(str(root/'test_images'/name));scale=min(1,args.max_side/max(bgr.shape[:2]));small=cv2.resize(bgr,None,fx=scale,fy=scale,interpolation=cv2.INTER_AREA)
        masks,scores,seconds=predict(model,small)
        truth=[coco_mask.decode(a['segmentation']).astype(bool) for a in data['annotations'] if a['image_id']==info['id'] and a['area']>=667]
        gt=[cv2.resize(a.astype(np.uint8),(small.shape[1],small.shape[0]),interpolation=cv2.INTER_NEAREST)>0 for a in truth]
        ids=[int(i) for i in np.argsort(-scores) if scores[i]>=.5 and masks[i].sum()/scale**2>=667]
        matched=set();pairs=[]
        for i in ids:
            choices=[(np.count_nonzero(masks[i]&g)/max(np.count_nonzero(masks[i]|g),1),j) for j,g in enumerate(gt) if j not in matched]
            if choices:
                iou,j=max(choices)
                if iou>=.5:matched.add(j);pairs.append({'query':i,'truth':j,'iou':iou})
        row={'image':name,'truth':len(gt),'predictions':len(ids),'matches':len(matched),'seconds':seconds,'pairs':pairs,'top_scores':sorted(scores.tolist(),reverse=True)[:5]}
        result['results'].append(row)
        np.savez_compressed(out/(Path(name).stem+'.npz'),masks=masks[ids],scores=scores[ids],scale=scale)
        canvas=small.copy()
        for g in gt:
            contours,_=cv2.findContours(g.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);cv2.drawContours(canvas,contours,-1,(0,255,0),1)
        for i in ids:
            contours,_=cv2.findContours(masks[i].astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);cv2.drawContours(canvas,contours,-1,(0,255,255),1)
        cv2.imwrite(str(out/(Path(name).stem+'.jpg')),canvas)
        for field in ['truth','predictions','matches','seconds']:result[field]=sum(r[field] for r in result['results'])
        (out/'results.json').write_text(json.dumps(result,indent=2));print(json.dumps(row),flush=True)
    print(json.dumps({key:value for key,value in result.items() if key!='results'}),flush=True)
