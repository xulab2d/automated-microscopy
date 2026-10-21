"""Pinned read-only completed chips shared by successive acquisition invocations.

Only completed scientific rasters can be referenced. New or interrupted chips
remain owned by the new invocation and use its normal writable directories.
"""
from pathlib import Path
import hashlib,json,re
from .model import SafetyError
from .vendor_lifecycle import file_hash

KIND='readonly_completed_chip_v1'
COMPLETE={'complete','complete_with_boundary_limits','complete_with_focus_limits'}


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _check(check):
    if check:check()


def _identity(folder,key,root):
    folder=Path(folder)
    if (not re.fullmatch(r'chip\d+',key) or not folder.is_absolute() or folder.resolve()!=folder
            or folder.name!=key or folder.parent.parent!=Path(root).resolve()
            or not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',folder.parent.name)):
        raise SafetyError('Completed-chip source must be an unchanged sibling managed run/chip directory.')
    return folder


def _completion(manifest,plan,key):
    count=plan.get('tile_count');completed=manifest.get('completed',{})
    if (manifest.get('state') not in COMPLETE or manifest.get('planned_area_acquired') is not True
            or manifest.get('finalization_errors') or type(count) is not int or count<1
            or manifest.get('tile_count')!=count or set(completed)!={f'tile_{i:06d}' for i in range(count)}
            or manifest.get('coverage_plan_fingerprint')!=plan.get('fingerprint')):
        raise SafetyError('Read-only chip source is not a complete committed raster: '+key)
    unsigned={k:v for k,v in plan.items() if k!='fingerprint'}
    if hashlib.sha256(json.dumps(unsigned,sort_keys=True).encode()).hexdigest()!=plan['fingerprint']:
        raise SafetyError('Completed-chip coverage plan fingerprint changed.')
    for name,row in completed.items():
        if not re.fullmatch('[a-f0-9]{64}',row.get('hashes',{}).get(f'tiles/{name}.tiff','')):
            raise SafetyError('Completed chip lacks committed raw hashes: '+name)


def _entry_once(folder,key,root,*,check=None):
    from .scan_continuation import process_alive_strict
    folder=_identity(folder,key,root);_check(check)
    source=folder.parent;request_hash=file_hash(source/'scan-request.json');status=read(source/'scan-status.json');worker=read(source/'worker-start.json')
    if (status.get('hardware_closed') is not True or status.get('id')!=source.name
            or status.get('state') not in ('stopped','failed','search_sampling_complete','chip_budget_reached','time_budget_reached')
            or status.get('request_sha256')!=request_hash or worker.get('id')!=source.name
            or worker.get('request_sha256')!=request_hash or worker.get('pid')!=status.get('pid')
            or process_alive_strict(worker.get('pid'))):
        raise SafetyError('Completed-chip source owner is not closed with its original request.')
    plan_path=source/'plans'/f'{key}.json';manifest=read(folder/'manifest.json');plan=read(plan_path)
    _completion(manifest,plan,key);_check(check)
    result={'kind':KIND,'source_run_id':source.name,'source_folder':str(folder),
        'source_request_sha256':request_hash,'source_status_sha256':file_hash(source/'scan-status.json'),
        'source_worker_sha256':file_hash(source/'worker-start.json'),
        'manifest_sha256':file_hash(folder/'manifest.json'),'coverage_sha256':file_hash(folder/'coverage-evidence.npz'),
        'plan_sha256':file_hash(plan_path),'coverage_plan_fingerprint':plan['fingerprint'],
        'tile_count':plan['tile_count'],'state':manifest['state'],'planned_area_acquired':True,
        'policy':'Read-only original committed raster; verify scientific artifact hashes on use; no raw copy or new acquisition.'}
    _check(check);return result


def _entry(folder,key,root,*,check=None):
    from .rectangle_handoff import _retry_read
    return _retry_read(lambda:_entry_once(folder,key,root,check=check),check=check)


def resolve_completed_chip(out,state,key,*,check=None):
    """Resolve a pinned completed source; absence keeps the ordinary local path."""
    out=Path(out).resolve();entry=state.get('completed_chip_sources',{}).get(key)
    if entry is None:return out/key
    if state.get('current_chip')==key or state.get('chip_states',{}).get(key,{}).get('state') not in COMPLETE:
        raise SafetyError('Referenced completed chip cannot be resumed or changed to an active chip.')
    pinned=read(out/'scan-request.json').get('rectangle_handoff',{}).get('completed_chip_sources',{}).get(key)
    if pinned!=entry:raise SafetyError('Completed-chip source differs from the immutable invocation request.')
    folder=_identity(entry.get('source_folder',''),key,out.parent)
    if folder.parent==out:raise SafetyError('Read-only completed source must belong to a previous invocation.')
    if _entry(folder,key,out.parent,check=check)!=entry:
        raise SafetyError('Pinned completed-chip source evidence changed.')
    local_plan=out/'plans'/f'{key}.json'
    if not local_plan.is_file() or file_hash(local_plan)!=entry['plan_sha256']:
        raise SafetyError('Copied coverage plan differs from the referenced completed chip.')
    return folder


def build_completed_chip_sources(donor,batch,*,check=None):
    """Pin only complete chips; no raw TIFF is read or copied by this operation."""
    donor=Path(donor).resolve();result={}
    for key,chip in batch.get('chip_states',{}).items():
        _check(check)
        if key==batch.get('current_chip') or chip.get('state') not in COMPLETE:continue
        if key in batch.get('completed_chip_sources',{}):
            folder=resolve_completed_chip(donor,batch,key,check=check)
        else:folder=donor/key
        result[key]=_entry(folder,key,donor.parent,check=check)
    return result
