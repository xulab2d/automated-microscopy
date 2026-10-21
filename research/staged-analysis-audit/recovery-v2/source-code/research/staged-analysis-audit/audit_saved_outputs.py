"""Read-only audit. Writes only beside this script; never changes production data."""
import os
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[key]='1'
from pathlib import Path
import json,hashlib,datetime,sys
import numpy as np,cv2
from PIL import Image,ImageDraw
cv2.setNumThreads(1)
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from flakefinder.imaging import load_frame,preview
OUT=Path(__file__).parent
CHIP=ROOT/'data/whole-chip-standardized-01/chip001'
ANALYSIS=CHIP/'analysis-staged-features'
read=lambda p:json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(name,value):
    (OUT/name).write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8')
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def panel(image,title,size=(420,370)):
    image=image.copy();image.thumbnail((size[0],size[1]-38))
    result=Image.new('RGB',size,'#202020');result.paste(image,((size[0]-image.width)//2,38))
    ImageDraw.Draw(result).text((7,8),title,fill='white');return result
def geometry_mask(manifest,metadata,shape):
    h,w=shape;matrix=np.asarray(manifest['transform']['matrix']);pos=metadata['position']
    poly=(np.asarray(manifest['plan']['polygon_um'])-[pos['x'],pos['y']])@np.linalg.inv(matrix).T+[(2464-1)/2,(2056-1)/2]
    poly*=np.array([w/2464,h/2056])
    mask=np.zeros((h,w),np.uint8);cv2.fillPoly(mask,[np.int32(np.round(poly))],1)
    # Equivalent physical edge exclusion, within one boundary-label pixel.
    radius=round(12/(metadata['pixel_size_um'][0]*2464/w))
    return cv2.erode(mask,np.ones((2*radius+1,2*radius+1),np.uint8))>0
def main():
    manifest=read(CHIP/'manifest.json');raw_analysis=(ANALYSIS/'analysis.json').read_bytes()
    analysis=json.loads(raw_analysis);catalog=read(ANALYSIS/'flakes.json')['flakes']
    snapshot={'snapshot_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
      'analysis_sha256':hashlib.sha256(raw_analysis).hexdigest(),'completed_tiles':analysis['completed_tiles'],
      'total_tiles':analysis['total_tiles'],'failed_tiles':analysis['failed_tiles'],'thin_screen_count':analysis['thin_screen_count']}
    write('snapshot.json',snapshot)
    results=[];panels=[]
    for failure in analysis['failed_tiles']:
        tile=failure['tile_key'].split('/')[-1];path=CHIP/'boundary-labels'/f'{tile}.npz'
        labels=np.load(path)['labels'];metadata=read(CHIP/'tiles'/f'{tile}.json')
        predicted=geometry_mask(manifest,metadata,labels.shape)
        substrate=labels==1;safe=cv2.erode(np.uint8(substrate),np.ones((7,7),np.uint8))>0
        n,_,stats,_=cv2.connectedComponentsWithStats(np.uint8(safe),8)
        result={**failure,'recorded_labels_sha256':sha(path),'label_hash_matches_manifest':sha(path)==manifest['completed'][tile]['hashes']['boundary-labels/'+tile+'.npz'],
          'recorded_label_shape':list(labels.shape),'substrate_fraction':float(substrate.mean()),'unknown_fraction':float((labels==0).mean()),
          'non_substrate_fraction':float((labels==2).mean()),'eroded_substrate_fraction':float(safe.mean()),
          'largest_eroded_substrate_component_fraction':float(max(stats[1:,4],default=0)/labels.size),
          'predicted_eroded_polygon_fraction':float(predicted.mean()),
          'predicted_mask_substrate_fraction':float(substrate[predicted].mean()) if predicted.any() else None,
          'predicted_mask_non_substrate_fraction':float((labels[predicted]==2).mean()) if predicted.any() else None,
          'substrate_outside_predicted_eroded_polygon_fraction':float((substrate&~predicted).sum()/max(substrate.sum(),1)),
          'equivalent_recorded_substrate_area_um2':float(substrate.sum()*labels.size**0*2464*2056/labels.size*np.prod(metadata['pixel_size_um']))}
        results.append(result)
        image=Image.open(CHIP/'tiles'/f'{tile}.jpg').convert('RGB');image.thumbnail((420,330))
        panels.append(panel(image,f'{tile[5:]} substrate {100*substrate.mean():.2f}%'))
    write('failed-fields.json',results)
    sheet=Image.new('RGB',(420*5,370*((len(panels)+4)//5)),'#202020')
    for i,p in enumerate(panels):sheet.paste(p,(i%5*420,i//5*370))
    sheet.save(OUT/'failed-fields-contact.jpg',quality=88)
    thin=[r for r in catalog if r['thin_reference_evidence'].get('compatible') is True]
    compact=[];thinpanels=[]
    for row in thin:
        tile=row['tile_id'];frame=load_frame(CHIP/'tiles'/f'{tile}.tiff')
        labels=cv2.imread(str(ANALYSIS/f'chip001__{tile}.instances.png'),cv2.IMREAD_UNCHANGED)
        x,y,w,h=row['bbox_px'];margin=70
        x0=max(x-margin,0);y0=max(y-margin,0);x1=min(x+w+margin,labels.shape[1]);y1=min(y+h+margin,labels.shape[0])
        crop=preview(frame.rgb[y0:y1,x0:x1],4095.);mask=np.uint8(labels[y0:y1,x0:x1]==row['instance_label'])
        contours,_=cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
        overlay=crop.copy();cv2.drawContours(overlay,contours,-1,(255,80,80),1)
        raw=Image.fromarray(crop);marked=Image.fromarray(overlay)
        pair=Image.new('RGB',(1120,740),'#202020');pair.paste(panel(raw,'Raw scientific RGB; fixed 4095 / gamma .6',(560,740)),(0,0));pair.paste(panel(marked,'Exact saved mask boundary',(560,740)),(560,0))
        pair.save(OUT/f'{tile}-{row["id"]}.jpg',quality=94)
        thinpanels.append(panel(raw,f'{tile[5:]} {row["area_um2"]:.0f} um2',(520,570)))
        small={k:v for k,v in row.items() if k not in ('polygon_px','holes_px','stage_polygon_um','focus_status','sieve_results')}
        small['source_hash_matches_manifest']=sha(CHIP/'tiles'/f'{tile}.tiff')==manifest['completed'][tile]['hashes']['tiles/'+tile+'.tiff']
        small['crop_bbox_xyxy']=[x0,y0,x1,y1];small['source_position']=manifest['completed'][tile]['position']
        compact.append(small)
    write('thin-matches.json',compact)
    sheet=Image.new('RGB',(1040,570*((len(thinpanels)+1)//2)),'#202020')
    for i,p in enumerate(thinpanels):sheet.paste(p,(i%2*520,i//2*570))
    sheet.save(OUT/'thin-matches-contact.jpg',quality=94)
    print(json.dumps({'snapshot':snapshot,'failed_stats':[{'tile':r['tile_key'],'substrate_fraction':round(r['substrate_fraction'],4),'prediction_substrate':r['predicted_mask_substrate_fraction']} for r in results]},indent=2))
if __name__=='__main__':main()
