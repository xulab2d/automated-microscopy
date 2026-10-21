"""Bounded one-CPU synthetic implementation check plus one saved blank control.

This does not estimate real graphene/hBN detection accuracy. No hardware access.
"""
import os
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[key]='1'
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--negative-frame',type=Path);parser.add_argument('--negative-labels',type=Path)
    args=parser.parse_args()
    if args.output.exists():raise FileExistsError('Use a new benchmark result file.')
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    from flakefinder.material_profiles import load_material_profile
    from flakefinder.material_contrast import MaterialContrastClassifier
    from flakefinder.material_terraces import segment_material_terraces
    records=[];rng=np.random.default_rng(20261009)
    for material in ('graphene','hbn'):
        profile=load_material_profile(ROOT/f'configs/materials/{material}-sio2-90nm.json')
        spacing=profile.config['layer_spacing_nm'];curve=profile.forward(np.arange(0,5)*spacing,illustrative=True)
        classifier=MaterialContrastClassifier(curve)
        contrast=rng.normal(0,.004,(1024,1024,3)).astype(np.float32);truth=np.zeros((1024,1024),np.int32)
        truth[140:380,130:430]=1;truth[560:900,600:920]=2;truth[200:240,200:240]=0
        for region,plane in ((1,1),(2,3)):contrast[truth==region]+=np.asarray(curve['contrast_rgb'][plane],np.float32)
        started=time.perf_counter()
        result=segment_material_terraces(contrast,np.ones(truth.shape,bool),truth==0,np.eye(3)*.004**2,(.2,.2),classifier)
        found=result['labels']>0;target=truth>0
        records.append({'kind':'synthetic_known_pixels','material':material,'pixels':truth.size,'seconds':time.perf_counter()-started,
            'pooled_foreground_iou':float((found&target).sum()/max((found|target).sum(),1)),
            'selected_regions':result['report']['selected_regions'],'all_source_regions':len(result['regions']),
            'source_hole_retained_fraction':float(np.mean(result['labels'][200:240,200:240]==0)),
            'source':'Synthetic profile-rendered fractional RGB contrast plus independent Gaussian noise; not real detection accuracy.',
            'report':result['report']})
    if args.negative_frame:
        if not args.negative_labels:raise ValueError('Saved independent substrate labels required for the negative frame.')
        from flakefinder.imaging import load_frame
        from flakefinder.model import Frame
        from flakefinder.terrace_domain import normalize_supported
        frame=load_frame(args.negative_frame);h,w=frame.rgb.shape[:2];ys=slice(h//2-384,h//2+384);xs=slice(w//2-384,w//2+384)
        with np.load(args.negative_labels,allow_pickle=False) as data:recorded=data['labels']
        support=cv2.resize(np.uint8(recorded==1),(w,h),interpolation=cv2.INTER_NEAREST)[ys,xs]>0
        crop=Frame(frame.rgb[ys,xs],frame.position,frame.pixel_size_um,frame.sequence,frame.acquired_at,frame.metadata)
        valid=np.ones(support.shape,bool);contrast,normalization=normalize_supported(crop,valid,support)
        residual=contrast[support];noise=np.cov(residual.T)+np.eye(3)*1e-12
        for material in ('graphene','hbn'):
            profile=load_material_profile(ROOT/f'configs/materials/{material}-sio2-90nm.json')
            curve=profile.forward(np.arange(0,6)*profile.config['layer_spacing_nm'],illustrative=True)
            started=time.perf_counter();result=segment_material_terraces(contrast,valid,support,noise,crop.pixel_size_um,MaterialContrastClassifier(curve),saturated_mask=np.any(crop.rgb>=crop.metadata['white_level'],axis=2))
            records.append({'kind':'saved_background_transfer_control','material_prior':material,'source':str(args.negative_frame.resolve()),
                'source_sha256':hashlib.sha256(args.negative_frame.read_bytes()).hexdigest(),'substrate_labels_sha256':hashlib.sha256(args.negative_labels.read_bytes()).hexdigest(),
                'crop_xywh':[xs.start,ys.start,768,768],'pixels':valid.size,'seconds':time.perf_counter()-started,
                'selected_regions':result['report']['selected_regions'],'all_source_regions':len(result['regions']),
                'normalization':normalization,'report':result['report'],
                'qualification':'Previously visually reviewed mostly bare MoTe2-batch substrate, not a labeled graphene/hBN field or a guaranteed particle-free blank. Negative-only sanity check; oxide mismatch is deliberately not target-domain validation.'})
    output={'schema':1,'cpu_threads':1,'records':records,'real_positive_validation_performed':False,
        'scope':'Synthetic algorithm checks and saved negative-control transfer only; physical profile and target-camera thickness accuracy unvalidated.'}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(output,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps([{k:v for k,v in record.items() if k in ('kind','material','material_prior','seconds','pooled_foreground_iou','selected_regions','all_source_regions')} for record in records],indent=2))


if __name__=='__main__':main()
