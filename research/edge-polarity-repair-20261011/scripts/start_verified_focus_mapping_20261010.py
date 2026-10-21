"""Retain measured focus and continue mapping after the corrected XY handoff."""
from pathlib import Path
import json,sys,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.scan_service import ScanService,write_json
from flakefinder.scan_continuation import build_contract,continuation_settings
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
service=ScanService(project);parent='scan-20261010-072501-e978e508'
marker=service.folder(parent)/'verified-focus-mapping-run.json'
if marker.exists():raise RuntimeError('The mapping continuation has already been submitted.')
contract=build_contract(project,parent,15484.)
planned=service.plan(continuation_settings(project,contract),continuation=contract,
                     focus_first={'reuse_verified_from':parent})
identifier=planned['run']['id']
write_json(service.folder(identifier)/'launch-review.json',{'at':time.time(),'parent_run':parent,
    'reason':'Fix software-only post-focus XY handoff; retain already measured and confirmed focus15684.14um.',
    'user_authorization':'Continue scan and find focus; no additional scope change.',
    'limits':contract['limits'],'deadline_at':contract['deadline_at'],'prior_data_preserved':True},exclusive=True)
result=service.start(identifier,release_hold=True)
write_json(marker,{'id':identifier,'at':time.time()},exclusive=True)
print(json.dumps(result),flush=True)
