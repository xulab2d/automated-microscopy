"""Resume a cooperatively closed, same-placement, pre-raster focus search.

No fixed Z window: the sole worker reads the live controller travel limits.
Prior images, STOP, frame identity and absolute deadline remain preserved.
Completed raster work must use campaign checkpoint continuation instead.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.scan_service import ScanService,read_json,write_json
from flakefinder.scan_continuation import build_contract,continuation_settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--prior',required=True)
    parser.add_argument('--start',action='store_true')
    parser.add_argument('--release-hold',action='store_true')
    args=parser.parse_args()
    service=ScanService(args.project);prior=service.folder(args.prior)
    batch=read_json(prior/'batch.json')
    if batch.get('chip_states') or list(prior.glob('chip*/manifest.json')):
        raise RuntimeError('Existing chip/raster work requires campaign checkpoint continuation; it must not be reset.')
    limits=read_json(prior/'scan-config.json')['limits']
    contract=build_contract(args.project,args.prior,limits['z_min'],z_max_um=limits['z_max'],focus_travel_policy='controller')
    focus_folder=prior/'initial-edge-focus'
    focus_paths=[focus_folder/name for name in ('focus-result.json','focused-full-field.tiff','focused-full-field.json')]
    if all(path.is_file() for path in focus_paths) and read_json(focus_paths[0]).get('success'):
        focus_first={'reuse_verified_from':args.prior,
            'source_sha256':{path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in focus_paths},
            'selection_policy':'Reuse hash-pinned successful focus from the verified same-placement parent; each chip still measures its own focus surface.'}
    else:
        candidates=[]
        for path in prior.glob('discovery/candidate*/edge-focus/attempt_*/selection/selection.json'):
            selection=read_json(path)
            if selection.get('status')=='failed':continue
            result=read_json(path.parent.parent/'focus-result.json',{})
            if not result.get('samples'):continue
            candidates.append((float(selection.get('orientation_coherence',0)),path))
        if not candidates:raise RuntimeError('No recorded current-placement edge to focus before discovery.')
        _,selection=max(candidates,key=lambda row:row[0])
        focus_first={'selection_path':str(selection),'selection_sha256':hashlib.sha256(selection.read_bytes()).hexdigest(),
                     'selection_policy':'Highest recorded edge orientation coherence in the verified same-placement parent; fresh ROI and focus are measured.'}
    planned=service.plan(continuation_settings(args.project,contract),continuation=contract,focus_first=focus_first)
    run_id=planned['run']['id'];run=service.folder(run_id)
    instruction=read_json(prior/'session-instruction.json',{})
    instruction.update(run_id=run_id,continued_from=args.prior,created_at=time.time(),specimen_moved=False,
        fresh_coordinate_frame_required=False,travel_scope='Original absolute XY; live controller Z travel; adaptive focus sampling.',
        prior_run_positions_valid=True,focus_range_user_authorized=True)
    write_json(run/'session-instruction.json',instruction,exclusive=True)
    write_json(prior/'supervision'/'adaptive-focus-continuation.json',{
        'schema':1,'new_run':run_id,'parent_request_sha256':contract['prior_files_sha256']['scan-request.json'],
        'new_request_sha256':hashlib.sha256((run/'scan-request.json').read_bytes()).hexdigest(),
        'original_coordinate_frame_id':contract['prior_coordinate_frame_id'],'deadline_at':contract['deadline_at'],
        'selection':focus_first,'created_at':time.time()},exclusive=True)
    outcome=service.start(run_id,release_hold=args.release_hold) if args.start else planned
    print(json.dumps({'run_id':run_id,'folder':str(run),'preflight_ok':planned['preflight']['ok'],
        'blockers':planned['preflight']['blockers'],'state':outcome['run']['state'],
        'pid':outcome['run'].get('pid'),'focus_first':focus_first,'deadline_at':contract['deadline_at']}),flush=True)


if __name__=='__main__':main()
