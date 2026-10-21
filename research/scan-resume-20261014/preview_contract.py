"""Read-only preparation of the no-copy handoff; plan() revalidates it later."""
from pathlib import Path
import json,sys,time
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT/'research/software-resume-20261014'))
from flakefinder.rectangle_handoff import build_contract
project=Path(json.loads((ROOT/'configs/storage.json').read_text())['project_root'])
started=time.monotonic()
contract=build_contract(project,'scan-20261013-221727-131d9ced',reference_completed=True)
with (HERE/'contract-preview.json').open('x',encoding='utf-8') as f:json.dump(contract,f,indent=2)
print(json.dumps({'seconds':time.monotonic()-started,'retained_chips':list(contract['completed_chip_sources']),
                  'remaining_files':len(contract['prior_files_sha256'])}),flush=True)
