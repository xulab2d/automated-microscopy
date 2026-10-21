"""Explicit immutable same-placement algorithm upgrade, without a new bootstrap.

The old run and STOP remain untouched. Scientific files are copied to a new
invocation; measured evidence keeps its hashes and original coordinate frame.
"""
from pathlib import Path
from datetime import datetime
import json,math,re,shutil,time

from .model import SafetyError,Limits,atomic_json
from .vendor_lifecycle import file_hash

KIND='same_run_rectangle_algorithm_upgrade_v1'
SCIENCE_FILES={'batch.json','layout.json','configuration.json','search-plan.json','reference-validation.json'}
SCIENCE_DIRS={'search','discovery','plans','reference','calibration','configuration-revisions'}
OWNER_FILES={'scan-request.json','scan-status.json','worker-start.json','scan-config.json',
             'camera-profile-used.json','reference-validation.json','STOP'}

def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def _checked(root,name):
    path=root/name
    if path.resolve()!=path or not path.is_relative_to(root):raise SafetyError('Handoff evidence is redirected.')
    return path

def _science(name):
    parts=Path(name).parts
    return name in SCIENCE_FILES or parts[0] in SCIENCE_DIRS or bool(re.fullmatch(r'chip\d+',parts[0]))

def build_contract(project,prior_run_id,*,process_checker=None):
    """Only a closed, absent owner can be pinned for the explicitly authorized upgrade."""
    from .scan_continuation import process_alive_strict
    project=Path(project).resolve();check=process_checker or process_alive_strict
    if not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',prior_run_id):raise SafetyError('Invalid handoff donor.')
    donor=_checked(project,'data/'+prior_run_id)
    status=read(donor/'scan-status.json');worker=read(donor/'worker-start.json');request=read(donor/'scan-request.json')
    if (status.get('hardware_closed') is not True or status.get('state') not in ('stopped','failed','search_sampling_complete','chip_budget_reached','time_budget_reached')
            or status.get('id')!=prior_run_id or worker.get('id')!=prior_run_id
            or status.get('pid')!=worker.get('pid') or check(worker.get('pid'))
            or status.get('request_sha256')!=file_hash(donor/'scan-request.json')
            or worker.get('request_sha256')!=status.get('request_sha256')):
        raise SafetyError('Handoff requires the exact closed and absent acquisition owner.')
    batch=read(donor/'batch.json');config=read(donor/'scan-config.json');validation=read(donor/'reference-validation.json')
    if not batch.get('coordinate_frame_id') or validation.get('accepted') is not True:
        raise SafetyError('Handoff requires the original accepted reference and coordinate frame.')
    reference=Path(batch['reference_path'])
    inherited_reference=request.get('rectangle_handoff',{}).get('reference_path')==str(reference)
    if (reference.resolve()!=reference or not reference.is_relative_to(project/'data')
            or not (reference.is_relative_to(donor) or inherited_reference) or file_hash(reference)!=batch['reference_sha256']
            or file_hash(reference)!=validation['raw_sha256']):raise SafetyError('Handoff reference changed.')
    started=datetime.fromisoformat(worker['started_at'].replace('Z','+00:00')).timestamp()
    deadline=request.get('rectangle_handoff',request.get('continuation',{})).get('deadline_at',started+request['settings']['hours']*3600)
    if not math.isfinite(deadline) or deadline<=time.time():raise SafetyError('Original acquisition deadline has passed.')
    names=sorted({p.relative_to(donor).as_posix() for p in donor.rglob('*') if p.is_file()
                  and (_science(p.relative_to(donor).as_posix()) or p.name in OWNER_FILES and p.parent==donor)})
    hashes={name:file_hash(_checked(donor,name)) for name in names}
    return {'schema':1,'kind':KIND,'prior_run_id':prior_run_id,'same_placement_authorized':True,
        'prior_files_sha256':hashes,'coordinate_frame_id':batch['coordinate_frame_id'],
        'search_bounds_um':batch['search_bounds_um'],'limits':config['limits'],'deadline_at':deadline,'reference_path':str(reference),
        'settings':request['settings'],'source_release':request['source_release'],
        'policy':'Copy original scientific evidence; resume saved candidates before pending lattice. No bootstrap or old-data overwrite.',
        'supplementary_candidate_budget':{'max_new_probes':4,'max_captures':64,'max_seconds':600.}}

def verify_contract(project,contract,*,process_checker=None):
    if not isinstance(contract,dict) or contract!=build_contract(project,contract.get('prior_run_id',''),process_checker=process_checker):
        raise SafetyError('Pinned rectangle handoff changed.')
    return contract

def source_config(project,contract):
    verify_contract(project,contract)
    donor=Path(project)/'data'/contract['prior_run_id']
    return read(donor/'scan-config.json'),read(donor/'camera-profile-used.json')

def prepare_run(project,out,contract):
    """Copy committed data only; invocation STOP/control files stay at their original paths."""
    verify_contract(project,contract);out=Path(out).resolve();donor=Path(project).resolve()/'data'/contract['prior_run_id']
    if out==donor or out.parent!=donor.parent:raise SafetyError('Handoff needs a separate managed invocation.')
    if (out/'batch.json').exists():raise SafetyError('Handoff destination already contains campaign state.')
    copied=[];preserved_stops=[]
    for name,digest in contract['prior_files_sha256'].items():
        if not _science(name):continue
        if Path(name).name=='STOP':preserved_stops.append(name);continue
        source=_checked(donor,name);target=_checked(out,name)
        if target.exists():raise SafetyError('Handoff would replace existing evidence: '+name)
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        if file_hash(target)!=digest:raise SafetyError('Handoff copy hash differs: '+name)
        copied.append(name)
    batch=read(out/'batch.json')
    # Do not rewrite raw frame metadata, chip plans, focus results or their fingerprints.
    for attempt in batch.get('boundary_attempts',[]):
        folder=Path(attempt['folder'])
        if folder.is_relative_to(donor):attempt['folder']=str(out/folder.relative_to(donor))
        if not attempt.get('chip_id'):
            attempt['pre_upgrade_state']=attempt['state'];attempt['state']='discovering'
            attempt['rectangle_reanalysis']=True
    for chip in batch.get('chip_states',{}).values():
        folder=Path(chip.get('discovery_folder',donor))
        if folder.is_relative_to(donor):chip['discovery_folder']=str(out/folder.relative_to(donor))
    batch.update(state='stopped',phase='prepared_rectangle_handoff',rectangle_handoff={
        'kind':KIND,'prior_run_id':donor.name,'deadline_at':contract['deadline_at'],
        'prior_batch_sha256':contract['prior_files_sha256']['batch.json']})
    atomic_json(out/'batch.json',batch)
    proof={'schema':1,'kind':KIND,'contract':contract,'copied_files':copied,
           'prior_stop_preserved':True,'copied_stop_controls_omitted':preserved_stops,
           'original_scientific_files_modified':False,'fresh_bootstrap':False}
    atomic_json(out/'rectangle-handoff-proof.json',proof)
    return batch

def verify_live(scope,profile,config,prior_profile,contract):
    """Under the new worker lease, validate actual pose and unchanged live optics/ranges."""
    if json.dumps(profile,sort_keys=True)!=json.dumps(prior_profile,sort_keys=True):
        raise SafetyError('Installed measured camera profile changed before handoff.')
    scope.check_cancel();position=scope.position();Limits(**config['limits']).check(position)
    controls=scope.hardware_controls();xy=controls.stage_travel_range();z=controls.focus_travel_range()
    limits=config['limits']
    values=[xy[k] for k in ('x_min_um','x_max_um','y_min_um','y_max_um')]+[z['min_um'],z['max_um']]
    if not all(type(v) in (int,float) and math.isfinite(v) for v in values):raise SafetyError('Invalid live controller travel.')
    if (limits['x_min']<values[0] or limits['x_max']>values[1] or limits['y_min']<values[2] or limits['y_max']>values[3]
            or limits['z_min']<values[4] or limits['z_max']>values[5]):raise SafetyError('Original envelope exceeds live controller limits.')
    if any(abs(xy.get(axis+'_position_um',float('inf'))-getattr(position,axis))>2 for axis in 'xy'):
        raise SafetyError('Live controller and adapter XY coordinate frames disagree.')
    optics=controls.objectives();selected=next((o for o in optics['objectives'] if o['position']==optics['position']),{})
    if optics['position']!=config['zen']['expected_objective_position'] or selected.get('magnification')!=20:
        raise SafetyError('Handoff requires the same manually engaged 20x objective.')
    return {'position_um':{a:getattr(position,a) for a in 'xyz'},'stage_range':xy,'focus_range':z,
            'position_policy':'Actual leased readback; last saved image is not asserted to be the final physical pose.'}

def run_handoff(scope,project,out,contract,config,profile,prior_profile,check,update,*,campaign_runner=None):
    from .config import arm
    from .chip_campaign import CampaignOptions,run_chip_campaign,validate_campaign_envelope
    runner=campaign_runner or run_chip_campaign
    proof=verify_live(scope,profile,config,prior_profile,contract);validate_campaign_envelope(config,contract['search_bounds_um'])
    arm(scope,config);check()
    lamp=scope.hardware_controls().set_lamp_percent(config['context']['illumination_percent'])
    if not math.isfinite(lamp) or abs(lamp-config['context']['illumination_percent'])>1:raise SafetyError('Handoff lamp differs from the saved reference.')
    scope.set_exposure_ms(config['context']['exposure_ms']);check()
    batch=prepare_run(project,out,contract);atomic_json(Path(out)/'rectangle-live-verification.json',proof)
    atomic_json(Path(out)/'scan-config.json',config)
    def progress(phase,frame,info):check();update(phase,progress={k:v for k,v in info.items() if k in ('index','total','state','reason','anchor_index','z_um')})
    common=dict(config=config,reference_tiff=batch['reference_path'],output=out,search_bounds_um=batch['search_bounds_um'],
        options=CampaignOptions(**batch['options']),full_chip_options=batch['full_chip_options'],scope=scope,callback=progress,allow_source_change=True)
    if batch.get('current_chip'):
        result=runner(**common,resume=True)
        if result.get('current_chip') or result.get('state')!='chip_complete_discovery_pending':return result
    check();remaining=contract['deadline_at']-time.time()
    if remaining<=0:raise SafetyError('Original acquisition deadline passed before campaign handoff.')
    return runner(**common,continue_discovery=True,continuation_walltime_s=remaining,rectangle_mode=True)
