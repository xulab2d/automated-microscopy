import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'software-next-20261009'))
from flakefinder.reference_repair import *
p=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy\data\scan-20261011-140151-bdf8664e')
plan,_=validate_plan(p);b=p/Path(PLAN).parent
proof=check_controls(load_frame(b/plan['focused_frame']),b,plan)
report=p.parent.parent/'research/session-20261011-geometry-continuity'
(report/'reference-control-replay.json').write_text(json.dumps(proof,indent=2));print(json.dumps(proof))
