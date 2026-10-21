"""Read saved six-field geometry and raw images; write separate halo QA only."""
from pathlib import Path
import argparse,json,sys,time,hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2,numpy as np
from PIL import Image,ImageDraw
from flakefinder.graphene_useful_regions import prepare_field
from flakefinder.hbn_halo_screen import screen_halos,OPTIONS,VERSION


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--project',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    run='scan-20261015-223549-98fae566';base=args.project/'analysis'/'hbn'/run/'chip001';source=base/'hbn-image-pilot-v6'
    summary=[];panels=[];known={'tile_001200':[1822],'tile_001700':[1282,1284,511]};positive=[]
    for folder in sorted((source/'fields').iterdir()):
        field=json.loads((folder/'field.json').read_text());tile=field['field'].split('/')[-1]
        a=np.load(folder/'masks.npz');labels=a['labels'];valid=a['valid_mask'];support=a['substrate_mask'];h,w=labels.shape
        rawpath=args.project/'data'/run/'chip001/tiles'/(tile+'.tiff')
        if hashlib.sha256(rawpath.read_bytes()).hexdigest()!=field['source_hashes']['tiles/'+tile+'.tiff']:raise ValueError('Raw source hash mismatch')
        raw=cv2.imread(str(rawpath),-1)[:,:,::-1];factor=field['reduction']['factor']
        rgb=raw[:h*factor,:w*factor].reshape(h,factor,w,factor,3).mean(axis=(1,3),dtype=np.float32)
        pixel=field['reduction']['boundary_resolution_um'];prepared=prepare_field(rgb,valid,support,pixel)
        started=time.perf_counter();result=screen_halos(field['regions'],labels,prepared,pixel,valid_mask=valid);seconds=time.perf_counter()-started
        flagged=[id for id,row in result.items() if row['likely_halo']]
        for id in known.get(tile,[]):positive.append({'tile':tile,'region':id,**result[id]})
        (args.output/(tile+'.json')).write_text(json.dumps(result))
        summary.append({'tile':tile,'flagged':flagged,'seconds':seconds});print(tile,len(flagged),'flagged',round(seconds,2),'s',flush=True)
        display=np.uint8(np.clip(rgb/np.percentile(rgb,99.5)*255,0,255))
        # Review every flagged source; never silently select the prettiest examples.
        for id in flagged:
            yy,xx=np.nonzero(labels==id);y0=max(0,yy.min()-20);y1=min(h,yy.max()+21);x0=max(0,xx.min()-20);x1=min(w,xx.max()+21)
            crop=display[y0:y1,x0:x1];overlay=crop.copy();mask=np.uint8(labels[y0:y1,x0:x1]==id)
            contours,_=cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE);cv2.drawContours(overlay,contours,-1,(255,255,0),1)
            panel=Image.new('RGB',(600,260),(23,28,32));d=ImageDraw.Draw(panel)
            metrics=result[id]['metrics'];d.text((5,3),f'{tile}/{id} core={metrics["interior_fraction"]:.2f} near={metrics["near_stronger_neighbor_fraction"]:.2f}',fill='white')
            for n,picture in enumerate((crop,overlay)):
                im=Image.fromarray(picture);im.thumbnail((290,230));panel.paste(im,(n*300,25))
            panels.append(panel)
    for start in range(0,len(panels),24):
        batch=panels[start:start+24];contact=Image.new('RGB',(1800,260*((len(batch)+2)//3)),(23,28,32))
        for i,panel in enumerate(batch):contact.paste(panel,((i%3)*600,(i//3)*260))
        contact.save(args.output/f'contact-{start//24+1}.jpg',quality=95)
    (args.output/'summary.json').write_text(json.dumps({'version':VERSION,'options':OPTIONS,'fields':summary,'known_positives':positive},indent=2))
    if any(r['observed_halo_conjunction'] for r in positive):raise AssertionError('Known positive has halo conjunction, even before override')


if __name__=='__main__':main()
