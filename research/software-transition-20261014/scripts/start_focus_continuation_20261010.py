"""User-directed lower-Z recovery and continuation, preserving original XY."""
from pathlib import Path
import json, sys, time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.scan_service import ScanService, write_json
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
service=ScanService(project)
parent='scan-20261010-025150-72aa2abf'
marker=service.folder(parent)/'focus-continuation-run.json'
if marker.exists():raise RuntimeError('This continuation has already been submitted.')
planned=service.plan_continuation(parent,z_min_um=15484.)
run_id=planned['run']['id'];request=service.request(run_id)
write_json(service.folder(run_id)/'launch-review.json',{
    'at':time.time(),'parent_run':parent,
    'authorization':'User directed finding focus and removing arbitrary travel restrictions in direct response to the proposed lower-Z extension; explicitly asked to use microscope-reported travel limits.',
    'previous_explicit_xy_approval':'Yes—I explicitly approve those exact limits; start the scan',
    'search_bounds_um':request['continuation']['search_bounds_um'],
    'limits':request['continuation']['limits'],
    'controller_range_check':'Live MTB focus limits checked before any motion.',
    'old_stops_preserved':True,
    'stop_control_and_hardware_error_handling_retained':True,
    'focus_change':'Downward sparse bracketing inside the proposed250um recovery interval; corrected mapping/AF shared budget starvation.',
},exclusive=True)
result=service.start(run_id,release_hold=True)
write_json(marker,{'id':run_id,'at':time.time(),'reason':'User-directed wider focus search; same XY frame and absolute deadline'},exclusive=True)
print(json.dumps(result),flush=True)
