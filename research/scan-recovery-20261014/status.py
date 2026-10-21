"""Compact read-only telemetry for the recovery invocation."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[2]
def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
project = Path(read(ROOT / 'configs/storage.json')['project_root'])
current = read(project / 'scan-control/current.json')
run = project / 'data' / current['id']
status = read(run / 'scan-status.json')
batch = read(run / 'batch.json')
print(json.dumps({'run_id': run.name,
    'status': {k: status.get(k) for k in ('state','phase','pid','updated_at','error','hardware_closed','progress','file_progress','memory_cleanup_launch')},
    'batch': {k: batch.get(k) for k in ('state','phase','current_chip','updated_at')},
    'chips': {k: {field: value.get(field) for field in ('state','completed_tiles','total_tiles')}
              for k,value in batch.get('chip_states',{}).items()},
    'memory_guard': read(run / 'memory-guard/status.json'),
    'memory_recovery': read(run / 'memory-recovery.json'),
    'log_tail': (run / 'worker.log').read_text(errors='replace')[-3000:] if status.get('state')=='failed' else None}, indent=2))
