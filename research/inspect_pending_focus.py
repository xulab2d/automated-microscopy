from pathlib import Path
import json
project=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
run=project/'data'/json.loads((project/'scan-control/current.json').read_text())['id']
for p in sorted((run/'observations').glob('*.json')):
    if p.name.endswith('.vendor.json'):continue
    d=json.loads(p.read_text())
    if d.get('observation_kind')=='quick_focus_sample':
        print(p.name,json.dumps(d));break
print('ATTEMPT',list((run/'discovery/candidate002/edge-focus/attempt_000').iterdir()))
