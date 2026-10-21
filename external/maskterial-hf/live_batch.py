"""Run an isolated learned-mask baseline on already acquired microscope images."""
from pathlib import Path
import argparse,json
import cv2,numpy as np,torch
from probe_cpu import load_model,predict

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--weights');p.add_argument('--output',default='external/maskterial-hf/live-synthetic1536');p.add_argument('--tiles',default='0,2,5,8,12');p.add_argument('--max-side',type=int,default=1536)
    args=p.parse_args();torch.set_num_threads(8);model=load_model(args.weights)
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True);result={'checkpoint':args.weights or 'synthetic pretrained','images':[],'status':'unvalidated MoTe2 proposals; neither material nor thickness is assigned'}
    for index in [int(i) for i in args.tiles.split(',')]:
        source=Path('data/20x-search-01/tiles')/f'tile_{index:06d}.jpg';meta=json.loads(source.with_suffix('.json').read_text())
        bgr=cv2.imread(str(source));scale=min(1,args.max_side/max(bgr.shape[:2]));small=cv2.resize(bgr,None,fx=scale,fy=scale,interpolation=cv2.INTER_AREA)
        masks,scores,seconds=predict(model,small);pixel_size=np.array(meta['pixel_size_um'])/scale;pixel_area=float(np.prod(pixel_size));rows=[];ids=[];canvas=small.copy()
        for i in np.argsort(-scores):
            if scores[i]<.5:continue
            mask=masks[i].astype(np.uint8);area=float(mask.sum()*pixel_area);width=float(2*cv2.distanceTransform(mask,cv2.DIST_L2,5).max()*min(pixel_size))
            if area<100 or width<6:continue
            contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);cv2.drawContours(canvas,contours,-1,(0,255,255),2)
            ids.append(int(i));rows.append({'query':int(i),'score':float(scores[i]),'area_um2':area,'usable_width_um':width})
        np.savez_compressed(output/(source.stem+'.npz'),masks=masks[ids],scores=scores[ids],scale=scale)
        cv2.imwrite(str(output/(source.stem+'.jpg')),canvas)
        row={'image':str(source),'seconds':seconds,'scale':scale,'candidates':rows,'top_scores':sorted(scores.tolist(),reverse=True)[:5]};result['images'].append(row)
        (output/'results.json').write_text(json.dumps(result,indent=2));print(json.dumps(row),flush=True)
