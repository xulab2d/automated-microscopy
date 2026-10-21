"""Bounded offline regression replay; never reads/writes active acquisition state."""
import os
os.environ['OMP_NUM_THREADS']='1'
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import cv2
import numpy as np

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT))
from flakefinder.imaging import load_frame
CAMPAIGN=ROOT/'data/whole-chip-standardized-01/discovery'
cv2.setNumThreads(1)
def module(name,file):
    spec=importlib.util.spec_from_file_location(name,HERE/file)
    item=importlib.util.module_from_spec(spec);sys.modules[name]=item;spec.loader.exec_module(item)
    return item
edges=module('flakefinder._research_edges','proposed_sparse_edges.py')
photo=module('_research_photo','photometric_validation.py')
fixtures=[('candidate010',i)for i in (6,9,10,12,17,25)]+[('candidate015',i)for i in (4,6,7,8,21,14,29)]+[
    ('candidate013',21),('candidate016',13)]+[('candidate005',i)for i in (4,7,10,20)]
start=time.perf_counter();rows=[];accepted_by_attempt={};all_observations={};data={}
for attempt,index in fixtures:
    folder=CAMPAIGN/attempt
    discovery=data.setdefault(attempt,json.loads((folder/'discovery.json').read_text()))
    record=discovery['records'][index]
    with np.load(folder/record['labels_file'])as stored:labels=stored['labels']
    edge=edges.coherent_edge(labels,record['position_um'],discovery['source_shape'],discovery['transform']['matrix'])
    frame=load_frame(folder/record['image'])
    edge,validation=photo.supported_edge(frame,labels,edge,discovery['transform']['matrix'],allow_raw_refinement=record.get('edge') is None) if edge else (None,dict(accepted=False,reason='No label proposal'))
    if validation['accepted']:
        edge.update(image=record['image'],phase=record['phase'],position_um=record['position_um'])
        accepted_by_attempt.setdefault(attempt,[]).append(edge)
    rows.append(dict(attempt=attempt,image=record['name'],originally_accepted=bool(record.get('edge')),
                     recorded_source_hashes=record['hashes'],validation=validation))
    print(attempt,record['name'],validation['accepted'],validation['reason'])
# Independent registered patch checks include all labels, not all raw images.
fits={}
for attempt in ('candidate010','candidate015','candidate005'):
    d=data[attempt];observations=[]
    for rec in d['records']:
        with np.load(CAMPAIGN/attempt/rec['labels_file'])as stored:labels=stored['labels'].copy()
        observations.append((rec['name'],labels,rec['position_um'][:2]))
    fit=edges.fit_outline(accepted_by_attempt.get(attempt,[]),d['seed_xy_um'],d['options']['search_bounds_um'],d['options']['margin_um'])
    checks=[photo.registered_substrate_consistency(line,observations,d['transform']['matrix'],d['source_shape'],d['options']['margin_um'])for line in fit.get('lines',[])]
    fits[attempt]={'fit':fit,'registered_patch_checks':checks,'all_registered_checks_pass':all(c['accepted']for c in checks)}
brackets=[]
for attempt,index in [('candidate014',1),('candidate013',11)]:
    d=data[attempt]if attempt in data else json.loads((CAMPAIGN/attempt/'discovery.json').read_text())
    rec=d['records'][index]
    with np.load(CAMPAIGN/attempt/rec['labels_file'])as stored:labels=stored['labels']
    brackets.append(dict(attempt=attempt,image=rec['name'],recorded_classification=rec['classification'],
                         proposed=photo.spatial_bracket_evidence(labels,rec['classification'])))
result=dict(schema=1,scope='Offline representative replay only; not production deployed or live validated.',
    source_sha256={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest()for name in ['photometric_validation.py','proposed_sparse_edges.py','replay_photometric.py']},
    discovery_sha256={name:hashlib.sha256((CAMPAIGN/name/'discovery.json').read_bytes()).hexdigest()for name in data},
    processing_seconds=time.perf_counter()-start,rows=rows,representative_model_fits=fits,bracket_examples=brackets)
(HERE/'photometric-representative-replay.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print('Representative fits',[(k,v['fit']['status'],len(v['fit'].get('lines',[])),v['all_registered_checks_pass'])for k,v in fits.items()])
