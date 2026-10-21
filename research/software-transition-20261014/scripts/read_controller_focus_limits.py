"""Read the current controller range and checkpoint; never arm or move."""
from pathlib import Path
from dataclasses import asdict
import json, sys, time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flakefinder.config import connect
from flakefinder.scan_service import ScanService, read_json, write_json

project = Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
service = ScanService(project)
current = service.snapshot()['run']
if current and current['process_alive']:
    raise RuntimeError('An acquisition worker still owns the hardware.')
prior = project/'data'/'scan-20261010-025150-72aa2abf'
scope = connect(read_json(prior/'scan-config.json'))
try:
    scope.lease.acquire()
    position = scope.position()
    controls = scope.hardware_controls()
    focus = controls.Continual(controls.root.GetComponent('MTBFocus'))
    result = dict(at=time.time(), position_um=asdict(position),
                  focus_min_um=float(focus.GetMinPosition('µm')),
                  focus_max_um=float(focus.GetMaxPosition('µm')),
                  focus_spec=controls.focus_motion_spec(),
                  objectives=controls.objectives(), illumination_percent=controls.lamp_percent(),
                  controller_configuration=str(controls.config_path), armed=False, motion=False)
    path=project/'scan-control'/('controller-range-'+time.strftime('%Y%m%d-%H%M%S')+'.json')
    write_json(path,result,exclusive=True)
    print(json.dumps(dict(path=str(path),**result)),flush=True)
finally:
    scope.close()
