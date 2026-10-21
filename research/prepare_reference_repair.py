from pathlib import Path
import json,hashlib,shutil,sys
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'research/software-next-20261009'))
from flakefinder.scan_continuation import process_alive_strict
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy');run=project/'data/scan-20261011-140151-bdf8664e'
read=lambda p:json.loads(p.read_text(encoding='utf-8-sig'));sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert read(project/'scan-control/current.json')['id']==run.name
assert read(run/'scan-status.json')['hardware_closed'] and not process_alive_strict(28352)
out=run/'supervision/reference-repair';out.mkdir(exist_ok=True)
focused=project/'data/scan-20261011-120259-a91de40d/initial-edge-focus'
assets={'focused.tiff':focused/'focused-full-field.tiff','focused.json':focused/'focused-full-field.json',
 'focus-result.json':focused/'focus-result.json','selection.json':focused/'selection/selection.json',
 'chip-a.tiff':run/'discovery/candidate003/frames/probe_000002.tiff',
 'chip-b.tiff':run/'discovery/candidate003/frames/probe_000015.tiff',
 'backing-a.tiff':run/'discovery/candidate003/frames/probe_000009.tiff',
 'backing-b.tiff':run/'discovery/candidate003/frames/probe_000010.tiff'}
for name,path in list(assets.items()):
 if path.suffix=='.tiff' and name!='focused.tiff':assets[name.replace('.tiff','.json')]=path.with_suffix('.json')
for name,path in assets.items():
 target=out/name
 if not target.exists():shutil.copy2(path,target)
 assert sha(path)==sha(target)
plan={'schema':1,'kind':'focused_reference_with_independent_controls_v1','prior_request_sha256':sha(run/'scan-request.json'),
 'reason':'Original reference was a defocused mixed boundary; independent bright chip interiors were mislabeled as holder and dark backing as substrate.',
 'focused_frame':'focused.tiff','focus_result':'focus-result.json','selection':'selection.json',
 'chip_controls':['chip-a.tiff','chip-b.tiff'],'backing_controls':['backing-a.tiff','backing-b.tiff'],
 'files_sha256':{name:sha(out/name) for name in assets},'original_sources':{name:str(p) for name,p in assets.items()},
 'position_um':read(out/'focused.json')['position'],'fresh_focus_score_fraction':.95,
 'qualification':'Correct optical chip/backing association; no material or layer identity inferred.'}
path=out/'plan.json'
if path.exists():assert read(path)==plan
else:path.write_text(json.dumps(plan,indent=2))
print(json.dumps({'plan':str(path),'position':plan['position_um'],'assets':len(assets)}))
