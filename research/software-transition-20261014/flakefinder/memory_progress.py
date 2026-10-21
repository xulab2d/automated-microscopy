"""Preserve pre-raster campaign progress in a verified memory continuation."""
from pathlib import Path
import json,shutil
from .model import SafetyError,Limits,atomic_json
from .chip_discovery import file_hash


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def checked(root,name):
    path=(root/name).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):raise SafetyError('Progress evidence escapes its run.')
    return path


def progress_files(folder,batch):
    """Bind metadata and every committed observation, before any new worker."""
    if not batch.get('seed_records') or batch.get('chip_states'):return []
    files=['search-plan.json']
    for record in batch['seed_records']:
        for name,expected in record.get('hashes',{}).items():
            path=checked(folder,name)
            if file_hash(path)!=expected:raise SafetyError('Saved campaign seed changed.')
            files.append(path.relative_to(folder).as_posix())
    for attempt in batch.get('boundary_attempts',[]):
        path=Path(attempt['folder']).resolve()
        if attempt.get('state')=='queued_reference_seed' and not path.exists():continue
        # Terminal historical attempts remain referenced in batch lineage.
        if not path.is_relative_to(folder.resolve()):
            if attempt.get('state') in ('discovering','failed','stopped'):raise SafetyError('Active discovery escapes its run.')
            continue
        files.append((path/'discovery.json').relative_to(folder).as_posix())
        state=read(path/'discovery.json')
        for record in state.get('records',[])+state.get('imported_records',[])+state.get('support_records',[]):
            for name,expected in record['hashes'].items():
                evidence=checked(path,name)
                if file_hash(evidence)!=expected:raise SafetyError('Saved geometric observation changed.')
                files.append(evidence.relative_to(folder).as_posix())
    return list(dict.fromkeys(files))


def prepare_progress(out,state,config,reference,transform,search):
    """Called only after the worker's stationary placement verification.

    Old evidence is copied into a new lineage, never mutated. A failed bounded
    operation receives one explicit 600-second recovery allowance, while its
    unused capture/probe allowances and the absolute session deadline remain.
    """
    request_path=out/'scan-request.json'
    if not request_path.exists():return None
    contract=read(request_path).get('continuation',{})
    if not contract.get('preserve_pre_raster_progress'):return None
    proof=read(out/'continuation-verification.json')
    if proof.get('continuation')!=contract:raise SafetyError('Progress requires the verified continuation contract.')
    from .startup_continuation import donor_contract
    contract=donor_contract(contract)
    if contract.get('prior_run_id')!=config.get('context',{}).get('prior_run_id'):
        raise SafetyError('Progress parent differs from the verified current placement.')
    parent=out.parent/contract['prior_run_id'];pinned=contract['prior_files_sha256']
    for name,expected in pinned.items():
        if file_hash(checked(parent,name))!=expected:raise SafetyError('Pinned progress evidence changed: '+name)
    old=read(parent/'batch.json')
    if old.get('chip_states') or old.get('current_chip') is not None:raise SafetyError('Pre-raster continuation cannot reset acquired chips.')
    if old['coordinate_frame_id']!=state['coordinate_frame_id'] or old['search_bounds_um']!=state['search_bounds_um']:
        raise SafetyError('Saved campaign coordinates differ.')
    if read(parent/'search-plan.json')!=search:raise SafetyError('Saved campaign search lattice differs.')
    seeds=json.loads(json.dumps(old['seed_records']))
    for record in seeds:
        for name,expected in record.get('hashes',{}).items():
            source=checked(parent,name);destination=out/name;destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists():raise SafetyError('Progress seed destination is not empty.')
            shutil.copy2(source,destination)
    if contract.get('reference_correction'):
        from .reference_repair import reclassify_progress
        return reclassify_progress(out,state,config,reference,transform,search,old,seeds)
    attempts=json.loads(json.dumps(old.get('boundary_attempts',[])));pending=[];resumed=[]
    from .sparse_import import prepare_discovery_import,digest
    for attempt in attempts:
        source=Path(attempt['folder'])
        if attempt.get('state')=='queued_reference_seed' and not source.exists():
            attempt['folder']=str(out/'discovery'/attempt['id'])
            pending.append((attempt['seed_record_index'],attempt));resumed.append(attempt['id']);continue
        if attempt.get('state') not in ('discovering','failed','stopped'):continue
        saved=read(source/'discovery.json')
        if saved.get('state') not in ('failed','stopped'):raise SafetyError('Expected interrupted discovery evidence.')
        captures=int(saved.get('operation_budget',{}).get('captures_started',0))
        maximum=int(saved.get('operation_budget',{}).get('max_captures',64))
        remaining=maximum-captures
        if remaining<=0:raise SafetyError('Interrupted discovery has no unused capture allowance.')
        destination=out/'discovery'/attempt['id']
        imported=prepare_discovery_import(source,destination,reference,transform,Limits(**config['limits']),
            config['chip_discovery']['edge_validation_profile'])
        if not imported['lineage']['new_acquisition_allowed']:raise SafetyError('Interrupted discovery has no unused probe allowance.')
        # Keep this run self-contained for subsequent verified continuations.
        for i,record in enumerate(imported['imported_records']):
            replacements={}
            for name,expected in record['hashes'].items():
                path=Path(name);relative=f'inherited/{i:06d}/{path.name}'
                target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
                replacements[name]=relative
            record['image']=replacements[record['image']];record['labels_file']=replacements[record['labels_file']]
            record['hashes']={replacements[n]:h for n,h in record['hashes'].items()}
            if record.get('edge'):record['edge']['image']=record['image']
        imported['context']={**config.get('context',{}),
            'restart_imported_records_sha256':digest(imported['imported_records']),
            'restart_probe_allowance':imported['lineage']['new_probe_allowance'],
            'restart_placement_status':'verified_current'}
        runtime={'fingerprint','state','started_at','updated_at','records','imported_records','rays','estimated_outline','lineage','edge_focus_attempts'}
        imported['fingerprint']=digest({k:v for k,v in imported.items() if k not in runtime})
        imported['lineage']['recovery_operation_budget']={'max_elapsed_s':600.,'max_total_captures':remaining,
            'prior_budget':saved.get('operation_budget'),'reason':'Typed pre-capture RAM failure after verified closure; one bounded continuation, original session deadline unchanged.'}
        atomic_json(destination/'discovery.json',imported)
        attempt.update(folder=str(destination),state='discovering',memory_progress_import=True,
            prior_discovery=str(source/'discovery.json'),recovery_operation_budget=imported['lineage']['recovery_operation_budget'])
        pending.append((attempt['seed_record_index'],attempt));resumed.append(attempt['id'])
    state.update(seed_records=seeds,boundary_attempts=attempts,
        progress_continuation={'prior_run':parent.name,'prior_elapsed_seconds':old.get('elapsed_seconds'),
            'preserved_seed_count':len(seeds),'resumed_discoveries':resumed,'old_files_modified':False})
    atomic_json(out/'progress-reused.json',state['progress_continuation'])
    seen={r['index'] for r in seeds if r.get('index') is not None}
    return {'pending':set(range(search['probe_count']))-seen,'discoveries':pending}
