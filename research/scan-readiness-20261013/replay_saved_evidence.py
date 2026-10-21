"""Read-only replay of the two known geometry failures; no hardware imports."""
import hashlib
import json
from pathlib import Path
import sys
import numpy as np

workspace=Path(__file__).resolve().parents[2]
source=workspace/'research/software-next-20261009'
sys.path.insert(0,str(source))
from flakefinder.full_chip import observed_edge_anchors, _anchor_points
from flakefinder.sparse_acquisition import _normal_recenter_target

project=Path(json.loads((workspace/'configs/storage.json').read_text())['project_root'])
run=project/'data/scan-20261011-155805-5c2c4a02'
plan_path=run/'chip001/coverage-plan.json'
plan=json.loads(plan_path.read_text())
folder=run/'discovery/candidate007'
discovery=json.loads((folder/'discovery.json').read_text())
anchors,evidence=observed_edge_anchors(discovery,folder,plan['scan_plan']['polygon_um'])
fallback=anchors is None
if fallback:anchors=_anchor_points(plan['support_points_um'],4)
folder=run/'discovery/candidate006'
discovery=json.loads((folder/'discovery.json').read_text())
record=discovery['records'][2]
with np.load(folder/record['labels_file']) as saved:
    target=_normal_recenter_target(saved['labels'],discovery['source_shape'],discovery['transform']['matrix'],
        record['position_um'],[1,0],discovery['seed_xy_um'],0,1500)
assert len(anchors)==4 and target is not None
result=dict(schema=1,historical_run=run.name,no_hardware=True,
    old_anchor_count=len(plan['anchor_xy_um']),new_anchor_count=len(anchors),
    substrate_fallback=fallback,edge_evidence=evidence,anchor_xy_um=anchors.tolist(),
    old_east_probe_xy_um=record['position_um'][:2],proposed_east_probe_xy_um=target.tolist(),
    qualification='Offline search proposals only. No live focus, edge acceptance, or new coverage is asserted.',
    source_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [source/'flakefinder/full_chip.py',source/'flakefinder/sparse_acquisition.py',plan_path,folder/'discovery.json']})
(Path(__file__).parent/'offline-replay.json').write_text(json.dumps(result,indent=2))
print(json.dumps({k:result[k] for k in ('old_anchor_count','new_anchor_count','substrate_fallback','proposed_east_probe_xy_um')}))
