"""Start the prepared same-placement continuation through the running console."""
from pathlib import Path
import json
import urllib.request

HERE = Path(__file__).resolve().parent
plan = json.loads((HERE / 'handoff-plan.json').read_text())
if not plan['preflight_ok']:
    raise RuntimeError('Saved plan has blockers')
if (HERE / 'handoff-start.json').exists():
    raise RuntimeError('Start already recorded; inspect current worker')
base = 'http://127.0.0.1:8770'
with urllib.request.urlopen(base + '/api/session', timeout=20) as response:
    token = json.load(response)['token']
request = urllib.request.Request(base + '/api/scan/start',
    data=json.dumps({'run_id': plan['run_id'], 'release_hold': True}).encode(),
    headers={'Content-Type': 'application/json', 'Origin': base, 'X-Review-Token': token})
with urllib.request.urlopen(request, timeout=120) as response:
    result = json.load(response)
run = result['run']
summary = {key: run.get(key) for key in ('id','state','phase','pid','process_alive')}
with (HERE / 'handoff-start.json').open('x', encoding='utf-8') as stream:
    json.dump(summary, stream, indent=2)
print(json.dumps(summary))
