"""Offline restoration of repaired feature masks plus existing RGB selection.

Reads saved images only. Two processes, each limited to two OpenCV/BLAS threads.
Existing analyses/raw acquisitions are preserved in separate output folders.
"""
import os
for variable in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[variable]='2'

import argparse
from concurrent.futures import ProcessPoolExecutor,as_completed
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def preflight(folder,profile):
    from flakefinder.analysis import tile_inputs
    from flakefinder.material_selection import optical_context,validate_frame_profile
    items=tile_inputs(folder)
    if not items:
        raise ValueError(f'No saved tiles: {folder}')
    for item in items:
        if optical_context(item['manifest'])!=profile['optical_context']:
            raise ValueError(f'Manifest optical context mismatch: {item["key"]}')
        sidecar=json.loads(item['path'].with_suffix('.json').read_text(encoding='utf-8'))
        validate_frame_profile(SimpleNamespace(metadata=sidecar['metadata']),profile)
    return {'tiles':len(items),'optical_context':'exact match','frame_metadata':'all matching'}


def worker(chip,base,report_dir,options):
    import cv2
    from threadpoolctl import threadpool_limits
    from flakefinder.analysis import analyze_scan
    from flakefinder.model import atomic_json,Cancelled
    cv2.setNumThreads(2)
    folder=Path(base)/chip;out=folder/'analysis-feature-contrast';reports=Path(report_dir)
    start=time.monotonic()
    def check_cancel():
        if (reports/'STOP').exists() or (out/'STOP').exists():
            raise Cancelled('Offline feature analysis stopped.')
    def progress(phase,detail):
        row={'chip':chip,'state':'running','phase':phase,**detail,'elapsed_s':time.monotonic()-start}
        atomic_json(reports/(chip+'.json'),row)
        with (reports/(chip+'.jsonl')).open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(row)+'\n')
        print(json.dumps(row),flush=True)
    try:
        check_cancel()
        with threadpool_limits(limits=2):
            result=analyze_scan(folder,options,output=out,callback=progress,check_cancel=check_cancel)
        row={'chip':chip,'state':'complete','elapsed_s':time.monotonic()-start,'output':str(out),
             **{k:result.get(k) for k in ('dataset_id','backend','distinct_images','raw_regions','candidate_count',
                 'rejected_count','deduplicated_count','focus_limited_tiles','thin_screen_count','material_selection')}}
    except Exception as exc:
        row={'chip':chip,'state':'failed','elapsed_s':time.monotonic()-start,'error':str(exc)}
        atomic_json(reports/(chip+'.json'),row)
        print(json.dumps(row),flush=True)
        raise
    atomic_json(reports/(chip+'.json'),row)
    print(json.dumps(row),flush=True)
    return row


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chips',nargs='+',default=['R1C2','R2C2','R2C3'])
    parser.add_argument('--preflight-only',action='store_true')
    args=parser.parse_args()
    from flakefinder.model import atomic_json
    base=ROOT/'data/chip-batch-01';reports=base/'feature-contrast-rebuild'
    reference=base/'R2C1/analysis-component-repair/analysis.json'
    profile_path=ROOT/'configs/mote2-285nm-20x-material.json'
    profile=json.loads(profile_path.read_text(encoding='utf-8'))
    options=json.loads(reference.read_text(encoding='utf-8'))['options']
    options.update(backend='empirical',material_profile=profile)
    checked={chip:preflight(base/chip,profile) for chip in args.chips}
    print(json.dumps({'preflight':checked}),flush=True)
    if args.preflight_only:
        return
    for chip in args.chips:
        if (base/chip/'analysis-feature-contrast/analysis.json').exists():
            raise ValueError(f'Completed destination already exists: {chip}; refusing to overwrite it.')
    reports.mkdir(exist_ok=True)
    if (reports/'STOP').exists():
        raise ValueError('STOP is present; remove it deliberately before restarting.')
    run={'state':'running','hardware_access':False,'worker_processes':2,'threads_per_worker':2,
         'preflight':checked,'options':options,'reference_analysis':str(reference),
         'profile_sha256':hashlib.sha256(profile_path.read_bytes()).hexdigest(),'results':[]}
    atomic_json(reports/'run.json',run);started=time.monotonic()
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures={pool.submit(worker,chip,str(base),str(reports),options):chip for chip in args.chips}
        for future in as_completed(futures):
            try:
                run['results'].append(future.result())
            except Exception as exc:
                run['results'].append({'chip':futures[future],'state':'failed','error':str(exc)})
            run['elapsed_s']=time.monotonic()-started
            atomic_json(reports/'run.json',run)
    run['state']='complete' if all(r['state']=='complete' for r in run['results']) else 'failed'
    atomic_json(reports/'run.json',run)
    if run['state']!='complete':
        raise SystemExit(1)


if __name__=='__main__':
    main()
