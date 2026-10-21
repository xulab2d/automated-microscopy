"""Deploy reviewed recovery after verifying the last worker closed unchanged."""
from pathlib import Path
import hashlib,json,shutil,sys
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
PROD=ROOT/'research/software-next-20261009'
STAGED=ROOT/'research/software-resume-20261014'
sys.path.insert(0,str(PROD))
from flakefinder.scan_continuation import process_alive_strict
from flakefinder.scan_service import source_files
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
project=Path(read(ROOT/'configs/storage.json')['project_root'])
rid='scan-20261014-090615-88fb3166';run=project/'data'/rid
status=read(run/'scan-status.json');worker=read(run/'worker-start.json');request=read(run/'scan-request.json')
if (read(project/'scan-control/current.json')['id']!=rid or status.get('hardware_closed') is not True
        or process_alive_strict(worker['pid']) or status.get('pid')!=worker['pid']
        or status.get('request_sha256')!=sha(run/'scan-request.json')
        or worker.get('request_sha256')!=status.get('request_sha256')):
    raise RuntimeError('Exact latest worker closure is not confirmed')
if source_files(PROD/'flakefinder')!=request['source_release']['files']:
    raise RuntimeError('Production changed since the last worker launch')
changes=[]
for directory in ('flakefinder','scripts','tests'):
    for source in sorted((STAGED/directory).rglob('*.py')):
        rel=source.relative_to(STAGED);target=PROD/rel
        before=sha(target) if target.exists() else None
        after=sha(source)
        if before!=after:changes.append({'path':rel.as_posix(),'before_sha256':before,'after_sha256':after})
report={'prior_run':rid,'files':changes,'deployed':False}
if '--deploy' in sys.argv:
    backup=HERE/'production-before';backup.mkdir(exist_ok=False)
    for item in changes:
        rel=Path(item['path']);source=STAGED/rel;target=PROD/rel
        if sha(source)!=item['after_sha256']:raise RuntimeError('Staging changed during deploy')
        if target.exists():
            saved=backup/rel;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved)
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        if sha(target)!=item['after_sha256']:raise RuntimeError('Deployed file differs')
    report['deployed']=True
    with (HERE/'deployment.json').open('x',encoding='utf-8') as f:json.dump(report,f,indent=2)
print(json.dumps(report,indent=2))
