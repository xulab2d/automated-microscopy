"""Explicit recovery from a diagnosed RAM watchdog stop, focus before mapping."""
from pathlib import Path
import hashlib,json,sys,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.scan_service import ScanService,write_json
from flakefinder.scan_continuation import build_contract,continuation_settings
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
service=ScanService(project);parent='scan-20261010-071637-83321549'
marker=service.folder(parent)/'direct-edge-continuation-run.json'
if marker.exists():raise RuntimeError('This explicit continuation is already submitted.')
contract=build_contract(project,parent,15484.)
selection=project/'data/scan-20261010-025150-72aa2abf/discovery/candidate003/edge-focus/attempt_000/selection/selection.json'
focus_first={'selection_path':str(selection),'selection_sha256':hashlib.sha256(selection.read_bytes()).hexdigest(),
             'purpose':'Fresh autofocus first at the recorded same-placement chip edge.'}
planned=service.plan(continuation_settings(project,contract),continuation=contract,focus_first=focus_first)
identifier=planned['run']['id']
write_json(service.folder(identifier)/'launch-review.json',{
    'at':time.time(),'parent_run':parent,'reason':'Diagnosed resource watchdog cancellation; actual hardware closure verified. User explicitly instructed managing RAM and proceeding to find focus.',
    'prior_stops_preserved':True,'ram_reserve_bytes':1024**3,'ram_launch_margin_bytes':int(1.5*1024**3),
    'limits':contract['limits'],'original_deadline_at':contract['deadline_at'],
    'first_action_after_reference':'Fresh edge registration and focus at saved edge XY before chip mapping.',
    'cleanup_log':'scan-control/memory-cleanup-20261010-0730.json',
    'xy_continuity':'Pinned settled motion journal plus live encoder verification before arm.'},exclusive=True)
result=service.start(identifier,release_hold=True)
write_json(marker,{'id':identifier,'at':time.time()},exclusive=True)
print(json.dumps(result),flush=True)
