"""Offline, versioned measured-background replay. Never writes acquisition data."""
from pathlib import Path
import argparse
import json
import sys
import time
import numpy as np
import cv2

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'research/software-next-20261009'))
from flakefinder.imaging import load_frame
from flakefinder.model import CameraTransform,atomic_json
from flakefinder.layout import SubstrateReference
from flakefinder.stage_background import KEY,build_model
from flakefinder.autonomous_reference import verify_reference
from flakefinder.photometric_edges import validate_photometric_edge
from flakefinder.chip_discovery import BoundaryOptions,classify_boundary
from flakefinder.sparse_edges import validated_coherent_edge
from flakefinder.vendor_lifecycle import file_hash


def replay(run,output):
    output.mkdir(parents=True,exist_ok=False)
    proof=json.loads((run/'autonomous-reference/proof.json').read_bytes())
    discovery=json.loads((run/'discovery/candidate001/discovery.json').read_bytes())
    reference_path=run/'calibration/substrate.tiff'
    reference=load_frame(reference_path)
    old=SubstrateReference.from_frame(reference)
    transform=CameraTransform(**discovery['transform'])
    edge_path=Path(proof['focused_edge_path'])
    assert file_hash(edge_path)==proof['focused_edge_sha256']
    assert file_hash(edge_path.with_suffix('.json'))==proof['focused_edge_metadata_sha256']
    edge=load_frame(edge_path)
    raw=validate_photometric_edge(edge,proof['edge_evidence']['edge'],transform.matrix)
    assert raw['accepted'],raw['reason']
    controls=verify_reference(reference,edge,proof['edge_evidence'],transform)
    model=build_model(reference,edge,{**proof['edge_evidence'],'raw_validation':raw},transform,controls,
        provenance={'edge_tiff_path':str(edge_path),'edge_tiff_sha256':file_hash(edge_path),
                    'edge_metadata_sha256':file_hash(edge_path.with_suffix('.json'))})
    reference.metadata[KEY]=model
    new=SubstrateReference.from_frame(reference)
    atomic_json(output/'model.json',model)
    options=BoundaryOptions(discovery['options']['search_bounds_um'])
    reference_rgb=np.asarray(controls['reference_median_rgb'])
    paths=[reference_path,edge_path]+[run/f'discovery/candidate001/frames/probe_{i:06d}.tiff' for i in [0,1,6,7,13,18,22,23,24]]
    rows=[]
    for index,path in enumerate(paths):
        frame=reference if path==reference_path else edge if path==edge_path else load_frame(path)
        before=classify_boundary(frame,old,reference_rgb,options)
        after=classify_boundary(frame,new,reference_rgb,options)
        def fractions(labels):return {name:float(np.mean(labels==value)) for value,name in enumerate(['unknown','substrate','stage_background_or_other_non_substrate'])}
        report={'path':str(path),'tiff_sha256':file_hash(path),'metadata_sha256':file_hash(path.with_suffix('.json')),
                'before':fractions(before),'after':fractions(after)}
        if path==edge_path:
            measured,validation=validated_coherent_edge(frame,after,transform.matrix,discovery['edge_validation_profile'])
            report['edge_validation']=validation
            report['accepted_measured_edge']=measured
        rows.append(report)
        palette=np.array([[95,95,95],[70,180,70],[230,125,40]],np.uint8)
        cv2.imwrite(str(output/f'labels-{index:02d}.png'),np.concatenate([palette[before],palette[after]],axis=1)[:,:,::-1])
        print(path.stem,report['before'],report['after'],flush=True)
        if frame is not reference and frame is not edge:del frame
    source_paths=[ROOT/'research/software-next-20261009/flakefinder'/name for name in
                  ['stage_background.py','layout.py','chip_discovery.py','autonomous_reference.py']]
    report={'schema':1,'run_id':run.name,'model_sha256':model['model_sha256'],
            'original_proof_sha256':file_hash(run/'autonomous-reference/proof.json'),
            'source_files':{str(p):file_hash(p) for p in source_paths},'rows':rows,
            'source_modified':False,'hardware_connected':False,
            'qualification':'Offline classification replay only; no chip outline or motion inferred. Independent original raw edge validator unchanged.'}
    atomic_json(output/'report.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();replay(args.run,args.output)
