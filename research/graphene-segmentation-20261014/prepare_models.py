"""Save conditional and external priors for this offline comparison."""
import os
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[key]='1'
import importlib.util,json,sys,time,shutil
from pathlib import Path
root=Path(__file__).resolve().parents[2];production=root/'research/software-next-20261009'
sys.path.insert(0,str(production))
from flakefinder.storage import resolve_output_path
project=Path(json.loads((root/'configs/storage.json').read_text())['project_root'])
output=resolve_output_path(project/'analysis/graphene/scan-20261013-221727-131d9ced/chip001/model-comparison')
output.mkdir(parents=True,exist_ok=True)
spec=importlib.util.spec_from_file_location('watcher',production/'scripts/watch_graphene_scan.py');watcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(watcher)
start=time.monotonic()
path=output/'conditional90.json'
if not path.exists():watcher.save(path,watcher.prepare_model(output))
external=production/'configs/materials/graphene90-2dmatgmm-external.json'
dest=output/'external90.json'
if not dest.exists():shutil.copyfile(external,dest)
model=json.loads(path.read_text());print(json.dumps({'output':str(output),'seconds':time.monotonic()-start,'keys':list(model),'contrast_shape_description':str(model.get('contrast_rgb'))[:300]},indent=2))
