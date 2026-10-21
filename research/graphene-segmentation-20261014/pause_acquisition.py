"""Record the user's acquisition pause; never connect to microscope hardware."""
import json
from pathlib import Path
from datetime import datetime, timezone
root=Path(__file__).resolve().parents[2]
project=Path(json.loads((root/'configs/storage.json').read_text())['project_root'])
evidence=Path(__file__).resolve().parent
paths=[root/'OPERATOR-HOLD.json',root/'research/software-next-20261009/OPERATOR-HOLD.json',project/'OPERATOR-HOLD.json']
hold={'acquisition_authorized':False,'reason':'User paused acquisition after manual takeover by another lab member; focus on offline graphene segmentation.',
      'recorded_date_local':'2026-10-14','recorded_at':datetime.now(timezone.utc).isoformat(),
      'allowed_work':'Offline analysis of saved scientific images, segmentation research, code and review UI improvements.',
      'next_acquisition_requires':'New user start instruction and fresh verification of objective, placement and coordinate frame after manual takeover. Do not automatically resume saved motion plans.'}
for index,path in enumerate(paths):
    backup=evidence/f'operator-hold-before-{index}.json'
    if backup.exists():raise RuntimeError('Pause already recorded; do not overwrite evidence')
    backup.write_bytes(path.read_bytes())
    path.write_text(json.dumps(hold,indent=2),encoding='utf-8')
print('Acquisition pause recorded at installation, production and shared project; no hardware accessed.')
