"""Bounded saved-data replay through the staged production APIs; no hardware."""
import os
os.environ['OMP_NUM_THREADS']='1'
from pathlib import Path
import hashlib,json,time
import cv2,numpy as np
from overlay import HERE,ROOT,profile_for
from review_ff.imaging import load_frame
from review_ff.sparse_edges import coherent_edge,validated_coherent_edge,fit_validated_outline
from review_ff.photometric_edges import ALGORITHM_VERSION,spatial_bracket_evidence

cv2.setNumThreads(1)
source=ROOT/'data/whole-chip-standardized-01/discovery'
reference_path=ROOT/'data/whole-chip-standardized-01/reference/substrate.tiff'
profile=profile_for(load_frame(reference_path))
profile['qualification']='Recorded 20x dark-holder representative replay only; not live validated.'
profile['reference_sha256']=hashlib.sha256(reference_path.read_bytes()).hexdigest()
(HERE/'manual20x-dark-holder-profile.example.json').write_text(json.dumps(profile,indent=2),encoding='utf-8')
fixtures=[('candidate010',i)for i in (6,9,10,12,17,25)]+[('candidate015',i)for i in (4,6,7,8,21,14,29)]+[('candidate013',21),('candidate016',13)]+[('candidate005',i)for i in (4,7,10,20)]
start=time.perf_counter();rows=[];data={};accepted={}
for attempt,index in fixtures:
    d=data.setdefault(attempt,json.loads((source/attempt/'discovery.json').read_text()))
    rec=d['records'][index];folder=source/attempt
    for name,digest in rec['hashes'].items():
        assert hashlib.sha256((folder/name).read_bytes()).hexdigest()==digest,(attempt,name)
    with np.load(folder/rec['labels_file'])as stored:labels=stored['labels'].copy()
    frame=load_frame(folder/rec['image'])
    old=coherent_edge(labels,rec['position_um'],d['source_shape'],d['transform']['matrix'])
    edge,validation=validated_coherent_edge(frame,labels,d['transform']['matrix'],profile)
    unchanged=None
    if rec.get('edge'):
        # The historical metadata fields were attached by acquisition, not fit.
        unchanged=edge is not None and all(edge.get(k)==v for k,v in old.items())
        assert unchanged,(attempt,rec['name'],'previously accepted geometry changed')
    if edge:
        edge.update(image=rec['image'],phase=rec['phase'],position_um=rec['position_um'])
        accepted.setdefault(attempt,[]).append(edge)
    rows.append({'attempt':attempt,'image':rec['name'],'recorded_source_hashes':rec['hashes'],
        'profile':profile,'previously_accepted_geometry_unchanged':unchanged,'edge':edge,'validation':validation})
    print(attempt,rec['name'],bool(edge),validation['reason'],flush=True)
fits={}
for attempt in ('candidate010','candidate015','candidate005'):
    d=data[attempt];observations=[]
    for rec in d['records']:
        with np.load(source/attempt/rec['labels_file'])as stored:labels=stored['labels'].copy()
        observations.append((rec['image'],labels,rec['position_um'][:2]))
    fits[attempt]=fit_validated_outline(accepted.get(attempt,[]),d['seed_xy_um'],d['options']['search_bounds_um'],
        d['options']['margin_um'],observations,d['transform']['matrix'],d['source_shape'])
brackets=[]
for attempt,index in [('candidate014',1),('candidate013',11)]:
    d=json.loads((source/attempt/'discovery.json').read_text());rec=d['records'][index]
    with np.load(source/attempt/rec['labels_file'])as stored:labels=stored['labels'].copy()
    brackets.append({'attempt':attempt,'image':rec['name'],'original':rec['classification'],
        'evidence':spatial_bracket_evidence(labels,rec['classification'])})
result={'schema':1,'algorithm':ALGORITHM_VERSION,'scope':'Offline integration replay; not live validated or deployed.',
    'runtime_s':time.perf_counter()-start,'profile':profile,'rows':rows,'fits':fits,'spatial_brackets':brackets,
    'discovery_sha256':{key:hashlib.sha256((source/key/'discovery.json').read_bytes()).hexdigest()for key in data},
    'staged_sources':json.loads((HERE/'source-manifest.json').read_text())['staged_source_sha256']}
(HERE/'replay-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print('FIT',[(key,v['status'],len(v.get('lines',[])),v['registered_consistency']['accepted'])for key,v in fits.items()])
print('SECONDS',result['runtime_s'])
