"""Deploy the progress follow-up only over the exact recorded first repair."""
from pathlib import Path
import hashlib
import json
import shutil
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
production = ROOT / 'research/software-next-20261009'
staged = ROOT / 'research/software-transition-20261014'
sys.path.insert(0, str(production))
from flakefinder.scan_continuation import process_alive_strict
from flakefinder.scan_service import source_files

def read(path): return json.loads(path.read_text(encoding='utf-8-sig'))
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()

project = Path(read(ROOT / 'configs/storage.json')['project_root'])
donor = project / 'data/scan-20261013-221727-131d9ced'
status, worker, request = [read(donor / name) for name in ('scan-status.json','worker-start.json','scan-request.json')]
if (status.get('hardware_closed') is not True or process_alive_strict(worker['pid'])
        or status.get('pid') != worker['pid'] or status.get('id') != donor.name
        or status.get('request_sha256') != digest(donor/'scan-request.json')
        or worker.get('request_sha256') != status.get('request_sha256')):
    raise RuntimeError('Exact donor closure/identity not confirmed')
if read(project / 'scan-control/current.json')['id'] != donor.name:
    raise RuntimeError('Current invocation changed before follow-up deployment')
expected = dict(request['source_release']['files'])
previous = read(HERE / 'deployment.json')
for change in previous['files']:
    path = Path(change['path'])
    if path.parts[0] == 'flakefinder':
        expected[path.relative_to('flakefinder').as_posix()] = change['after_sha256']
if source_files(production/'flakefinder') != expected:
    raise RuntimeError('Production differs from recorded first repair')
changes = []
for directory in ('flakefinder','tests'):
    for source in sorted((staged/directory).rglob('*.py')):
        relative = source.relative_to(staged)
        target = production/relative
        before = digest(target) if target.exists() else None
        if before != digest(source):
            changes.append({'path': relative.as_posix(), 'before_sha256': before, 'after_sha256': digest(source)})
if not changes: raise RuntimeError('No progress follow-up changes')
allowed = {'flakefinder/rectangle_handoff.py','flakefinder/scan_worker.py',
           'tests/test_rectangle_handoff.py','tests/test_scan_worker.py'}
if any(c['path'] not in allowed for c in changes):
    raise RuntimeError('Unexpected source change')
if '--deploy' in sys.argv:
    backup = HERE / 'production-before-progress'
    backup.mkdir(exist_ok=False)
    for change in changes:
        relative = Path(change['path'])
        source, target, saved = staged/relative, production/relative, backup/relative
        if digest(source) != change['after_sha256']: raise RuntimeError('Staging changed')
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, saved)
        shutil.copy2(source, target)
        if digest(target) != change['after_sha256']: raise RuntimeError('Deployment differs')
    with (HERE/'deployment-progress.json').open('x', encoding='utf-8') as stream:
        json.dump({'files': changes, 'deployed': True, 'prior_deployment_sha256': digest(HERE/'deployment.json')}, stream, indent=2)
print(json.dumps({'files': changes, 'deployed': '--deploy' in sys.argv}, indent=2))
