"""Offline positive-control diagnostic on MaskTerial's real MoSe2 images.

Run explicitly; not a unit test. This evaluates annotated terrace coverage, not
MoTe2 generalization. Bulk objects are not fully annotated, so unmatched generic
object proposals must not be reported as a calibrated false-positive rate.
"""
from pathlib import Path
import argparse,json
import cv2
import numpy as np
from flakefinder.model import Frame,Position,AcquisitionError
from flakefinder.colors import AnalysisOptions,ColorGroups,prepare_contrast
from flakefinder.analysis import extract_regions


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--split',choices=['train','test'],default='train')
    parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--output',default='data/public-positive-development.json')
    args=parser.parse_args()
    root=Path('external/benchmarks');options=AnalysisOptions();results=[]
    # Dataset documentation gives 200 pixels ~= 30 µm². This approximate scale
    # is adequate for a public-domain positive check, not device metrology.
    pixel=float(np.sqrt(.15))
    for path in sorted((root/(args.split+'_images')).glob('*.jpg'))[:args.limit]:
        frame=Frame(cv2.imread(str(path))[:,:,::-1],Position(0,0,0),(pixel,pixel),0)
        try:prep=prepare_contrast(frame,options)
        except AcquisitionError as exc:
            print(path.name,'analysis failed',str(exc),flush=True)
            continue
        groups=ColorGroups.fit(prep['smooth'][prep['foreground']],options.max_components)
        rows,_=extract_regions(frame,prep,groups,options)
        target=cv2.imread(str(root/(args.split+'_semantic_masks')/(path.stem+'.png')),cv2.IMREAD_UNCHANGED)
        truth=[]
        for label in np.unique(target):
            if not label:continue
            n,labels,stats,_=cv2.connectedComponentsWithStats(np.uint8(target==label),8)
            truth.extend(labels==j for j in range(1,n) if stats[j,cv2.CC_STAT_AREA]*pixel**2>=options.min_area_um2)
        matches=[]
        for mask in truth:
            best=0.;accepted=0.;best_reason=[]
            for row in rows:
                for region in [row]+row['terraces']:
                    got=np.zeros(target.shape,np.uint8)
                    cv2.fillPoly(got,[np.int32(region['polygon_px'])],1)
                    iou=float(np.logical_and(got,mask).sum()/max(np.logical_or(got,mask).sum(),1))
                    if iou>best:best=iou;best_reason=row['filter_reasons']
                    if row['disposition']=='candidate':accepted=max(accepted,iou)
            matches.append({'truth_area_um2':float(mask.sum()*pixel**2),
                            'best_any_iou':best,'best_accepted_iou':accepted,'best_reasons':best_reason})
        record={'image':path.name,'objects':rows,'matches':matches}
        results.append(record)
        print(path.name,'truth',len(truth),'accepted',sum(r['disposition']=='candidate' for r in rows),
              'matched',sum(m['best_accepted_iou']>=.5 for m in matches),flush=True)
        all_matches=[m for r in results for m in r['matches']]
        output={'dataset':'MaskTerial Real_MoSe2','source':'https://zenodo.org/records/15765514',
                'zip_md5':'c8054f220bdab0e01fbb903e9bd81aac','split':args.split,
                'approximate_pixel_area_um2':.15,'images':len(results),'truth_regions':len(all_matches),
                'matched_any':sum(m['best_any_iou']>=.5 for m in all_matches),
                'matched_accepted':sum(m['best_accepted_iou']>=.5 for m in all_matches),
                'iou_threshold':.5,'results':results}
        Path(args.output).write_text(json.dumps(output,indent=2))


if __name__=='__main__':main()
