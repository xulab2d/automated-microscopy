"""Read-only source comparison; fresh image-only derivatives, no selectors/hardware."""
from pathlib import Path
import argparse,json,sys,os,time
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):os.environ[key]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2,numpy as np
from PIL import Image,ImageDraw
from flakefinder.material_input import prepare_material_input
from flakefinder.hbn_image_regions import image_regions
from analyze_graphene_run import reduce_arrays


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--tiles',default='tile_001200,tile_001700')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    run='scan-20261015-223549-98fae566';chip=args.project/'data'/run/'chip001'
    pilot=args.project/'analysis'/'hbn'/run/'chip001'/'hbn-pilot-v2'
    examples={'tile_001200':[1189,925,769,956],'tile_001700':[307,83,546,550]}
    panels=[];measurements=[]
    for tile in args.tiles.split(','):
        folder=next((pilot/'fields').glob(tile+'-*'));field=json.loads((folder/'field.json').read_text())
        old=np.load(folder/'masks.npz');cache=args.output/(tile+'-input.npz')
        if cache.exists():arrays=dict(np.load(cache))
        else:
            arrays,provenance=prepare_material_input(chip_folder=chip,tile_id=tile,context=field['prepared_input']['context'],maximum_pixels=6000000)
            arrays,reduction=reduce_arrays(arrays,2)
            np.savez_compressed(cache,**arrays)
        started=time.perf_counter()
        result=image_regions(arrays['contrast_rgb'],arrays['valid_mask']&~arrays['saturated_mask'],arrays['substrate_mask'],arrays['noise_covariance'],arrays['pixel_size_um'],old['parent_labels'])
        np.savez_compressed(args.output/(tile+'-masks.npz'),labels=result['labels'],parent_labels=result['parent_labels'])
        (args.output/(tile+'-regions.json')).write_text(json.dumps({k:v for k,v in result.items() if k not in ('labels','parent_labels')}))
        print(tile,len(result['regions']),'regions',round(time.perf_counter()-started,2),'s',flush=True)
        raw=cv2.imread(str(chip/'tiles'/(tile+'.tiff')),cv2.IMREAD_UNCHANGED)
        # Same linear display scale across raw/mask panels; never radiometry.
        rgb=cv2.cvtColor(raw,cv2.COLOR_BGR2RGB).astype(float)
        rgb=np.uint8(np.clip(rgb/np.percentile(rgb,99.5)*255,0,255))
        for region_id in examples.get(tile,[]):
            region=next(r for r in field['regions'] if r['instance_label']==region_id)
            parent=region['parent_instance_label'];pm=old['parent_labels']==parent
            ys,xs=np.nonzero(pm);x0=max(0,int(xs.min())*2-30);x1=min(rgb.shape[1],int(xs.max())*2+32)
            y0=max(0,int(ys.min())*2-30);y1=min(rgb.shape[0],int(ys.max())*2+32)
            lab=cv2.resize(result['labels'],(rgb.shape[1],rgb.shape[0]),interpolation=cv2.INTER_NEAREST)
            target=np.isin(lab,np.unique(result['labels'][pm]));crop=rgb[y0:y1,x0:x1].copy();overlay=crop.copy()
            ids=np.unique(lab[y0:y1,x0:x1][target[y0:y1,x0:x1]])
            palette=[(255,255,0),(255,80,120),(0,255,255),(100,255,100),(160,100,255)]
            rows=[]
            for i,label in enumerate(ids):
                if not label:continue
                mask=np.uint8(lab[y0:y1,x0:x1]==label)
                contours,_=cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
                cv2.drawContours(overlay,contours,-1,palette[i%len(palette)],1)
                r=next(r for r in result['regions'] if r['instance_label']==label)
                if r['area_um2']>1:rows.append({'new_id':int(label),'area_um2':r['area_um2']})
            measurements.append({'tile':tile,'old_region':region_id,'parent':parent,'new_regions':rows})
            panel=Image.new('RGB',(800,330),(25,30,35));d=ImageDraw.Draw(panel)
            d.text((8,5),f'{tile} old {region_id} parent {parent}: raw / image-only masks',fill='white')
            for index,picture in enumerate((crop,overlay)):
                im=Image.fromarray(picture);im.thumbnail((390,290));panel.paste(im,(index*400,32))
            panels.append(panel)
    contact=Image.new('RGB',(1600,330*((len(panels)+1)//2)),(25,30,35))
    for i,panel in enumerate(panels):contact.paste(panel,((i%2)*800,(i//2)*330))
    contact.save(args.output/'contact.jpg',quality=95)
    (args.output/'examples.json').write_text(json.dumps(measurements,indent=2))


if __name__=='__main__':main()
