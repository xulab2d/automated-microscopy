"""File-only handoff publication. Does not import production code or control hardware."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib, json, os, shutil, tomllib

workspace = Path(__file__).resolve().parent.parent
project = Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy')
destination = project / 'research/session-20261011-handoff'
destination.mkdir(parents=True, exist_ok=True)
configuration = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))) / 'automations/overnight-chip-scan/automation.toml'
raw = configuration.read_bytes()
config = tomllib.loads(raw.decode('utf-8-sig'))
archive = destination / 'automation-before-handoff.toml'
if not archive.exists(): archive.write_bytes(raw)
prompt_archive = destination / 'historical-heartbeat-prompt.txt'
if not prompt_archive.exists(): prompt_archive.write_text(config['prompt'], encoding='utf-8')
source = workspace / 'research/NEW_CHAT_HANDOFF.txt'
target = destination / 'HANDOFF.txt'
target.write_bytes(source.read_bytes())
current = json.loads((project/'scan-control/current.json').read_text(encoding='utf-8-sig'))
run = project/'data'/current['id']
status = json.loads((run/'scan-status.json').read_text())
snapshot = run/'supervision/low-memory-snapshot.json'
if snapshot.exists(): shutil.copyfile(snapshot, destination/'latest-observation.json')
coordination = destination/'coordination.json'
if coordination.exists(): raise RuntimeError('Ownership record already exists; inspect it, do not overwrite a claimed owner.')
record = {
 'schema':1, 'updated_at':datetime.now(timezone.utc).isoformat(),
 'state':'awaiting_new_chat_takeover', 'previous_coordinator_thread_id':config['target_thread_id'],
 'coordinator_thread_id':None, 'acquisition_owner_agent':None,
 'old_chat_policy':'Read-only monitoring during user-requested handoff; no production/source/hardware/request mutations. After new owner claims, do not resume work here.',
 'takeover_authorization':'User explicitly requested a fresh conversation handoff and coordinator plus specialist agents. New chat claims on receiving pasted takeover instruction.',
 'automation_id':config['id'], 'handoff_path':str(target),
 'handoff_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
 'observed_run':current['id'], 'observed_state':status['state'],
 'observed_error':status.get('error'), 'hardware_closed':status.get('hardware_closed'),
 'new_agents_started':False,
}
with coordination.open('x', encoding='utf-8') as stream: json.dump(record, stream, indent=2)
failure = {'at':record['updated_at'], 'state':'failed_hardware_closed_handoff_pending',
 'run_id':current['id'], 'error_type':status.get('error_type'), 'error':status.get('error'),
 'hardware_closed':status.get('hardware_closed'), 'complete_chip_outlines':0, 'committed_raster_tiles':0,
 'reference_correction_live_verified':False, 'next_action':'New coordinator to diagnose retained vendor jobs and missing acknowledgements, validate exact continuation, then resume.',
 'coordination':str(coordination)}
(run/'supervision/handoff-status.json').write_text(json.dumps(failure, indent=2), encoding='utf-8')
(destination/'current-state.json').write_text(json.dumps(failure, indent=2), encoding='utf-8')
print(json.dumps({'handoff':str(target),'sha256':record['handoff_sha256'],'words':len(target.read_text().split()),
 'coordination':str(coordination),'automation_fields':{k:v for k,v in config.items() if k!='prompt'},
 'archived_prompt_chars':len(config['prompt'])}))
