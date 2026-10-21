"""Continue the same authorized scan after the diagnosed preview-path repair."""
from pathlib import Path
import json,sys,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.scan_service import ScanService,write_json
from flakefinder.scan_continuation import build_contract,continuation_settings

project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
service=ScanService(project);parent='scan-20261010-073149-12b69237'
marker=service.folder(parent)/'preview-repair-mapping-run.json'
if marker.exists():raise RuntimeError('The preview-repair continuation has already been submitted.')
contract=build_contract(project,parent,15484.)
planned=service.plan(continuation_settings(project,contract),continuation=contract,
    focus_first={'reuse_verified_from':'scan-20261010-072501-e978e508'})
identifier=planned['run']['id']
write_json(service.folder(identifier)/'launch-review.json',{
    'at':time.time(),'parent_run':parent,
    'reason':'Repair Windows long vendor-preview and atomic-temporary paths; confirmed focus retained.',
    'user_authorization':'Continue the scan, find focus, and manage dispensable RAM consumers automatically.',
    'limits':contract['limits'],'deadline_at':contract['deadline_at'],'prior_data_preserved':True},exclusive=True)
result=service.start(identifier,release_hold=True)
write_json(marker,{'id':identifier,'at':time.time()},exclusive=True)
print(json.dumps(result),flush=True)
