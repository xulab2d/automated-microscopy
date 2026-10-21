"""Explicit reviewed retention of a completed observation whose preview copy failed.

This is not a raw acknowledgement or a deletion policy. Unknown/canceled jobs
remain pending. Originals and observation evidence are checked on every capture.
"""
from pathlib import Path
import json,re,stat
from .model import SafetyError

REVIEW_KIND='retained_completed_observation_after_preview_failure_v1'
CROP_REVIEW_KIND='retained_completed_focus_crop_after_owner_stop_v1'
STOPPED_CAPTURE_REVIEW_KIND='retained_returned_bootstrap_capture_after_stop_v1'
CAMPAIGN_STOP_REVIEW_KIND='retained_export_at_closed_campaign_stop_v1'

def read(path):
    from .vendor_lifecycle import _review_hashes
    cache=_review_hashes.get()
    load=lambda p:Path(p).read_text(encoding='utf-8-sig')
    return json.loads(cache.read_text(path,load) if cache is not None else load(path))

def direct(path):
    path=Path(path)
    info=path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 or path.resolve()!=path.absolute():
        raise SafetyError('Retained vendor evidence is redirected.')
    return path

def make_review(root,observation):
    """Build reproducible evidence only; never writes, acknowledges, or removes."""
    from .vendor_lifecycle import file_hash
    from .scan_continuation import _preview_persistence_failure
    root=direct(root);project=root.parent.parent
    observation=direct(observation)
    if observation.stat().st_size>2*1024**2:raise SafetyError('Observation receipt exceeds its bound.')
    evidence=read(observation);capture=evidence.get('capture',{});identifier=capture.get('capture_id','')
    if not re.fullmatch('[a-f0-9]{32}',identifier):raise SafetyError('Invalid retained capture identity.')
    run=next((p for p in observation.parents if p.parent==project/'data' and p.name.startswith('scan-')),None)
    if run is None:raise SafetyError('Observation has no managed run provenance.')
    status=read(direct(run/'scan-status.json'))
    if status.get('hardware_closed') is not True:raise SafetyError('Observation owner closure is unproved.')
    proof=_preview_persistence_failure(run,status)
    if not proof or proof['capture_id']!=identifier or (run/proof['receipt'])!=observation:
        raise SafetyError('Retained review requires the exact completed/unloaded preview-persistence failure.')
    folder=direct(root/identifier)
    if Path(capture['result_folder'])!=folder:raise SafetyError('Vendor folder differs from observation provenance.')
    inventory=[]
    for path in sorted(folder.iterdir()):
        direct(path)
        if not path.is_file() or path.suffix.lower() not in ('.czi','.czjob','.png','.metadata'):
            raise SafetyError('Unexpected retained vendor inventory entry.')
        inventory.append({'name':path.name,'bytes':path.stat().st_size,'sha256':file_hash(path)})
    if sum(p['name'].lower().endswith('.czi') for p in inventory)!=1:
        raise SafetyError('Reviewed observation requires exactly one retained CZI.')
    source_files={str(path.relative_to(project).as_posix()):file_hash(direct(path)) for path in
                  (observation,run/'scan-status.json',run/'scan-request.json',run/'scan-config.json',run/'batch.json',Path(evidence['preview']['path']))}
    return {'schema':1,'kind':REVIEW_KIND,'capture_id':identifier,'capture':capture,
            'observation':observation.relative_to(project).as_posix(),'source_files_sha256':source_files,
            'inventory':inventory,'diagnosis':proof,'disposition':'retain_original_vendor_files',
            'raw_acknowledged':False,'scientific_commit':False,'placement_evidence':False}


def make_crop_review(root,crop,owner_run):
    """Review a completed capture retained after its closed owner stopped.

    A saved focus crop is NOT a full-raw duplicate. Keep the original CZI and
    every vendor companion in place. This narrow path requires the exact
    storage-continuation invocation that owned the campaign output, complete
    capture provenance, and a stop receipt. Unknown/canceled jobs do not qualify.
    The caller checks process/lease absence before writing the returned review.
    """
    from datetime import datetime
    import hashlib,math
    import numpy as np
    from .imaging import load_frame
    from .vendor_lifecycle import file_hash
    root=direct(root);project=root.parent.parent
    crop=direct(crop);owner_run=direct(owner_run)
    if root!=project/'data'/'zen-captures' or owner_run.parent!=project/'data' or not owner_run.name.startswith('scan-'):
        raise SafetyError('Retained crop review requires canonical managed storage and owner.')
    campaign=next((p for p in crop.parents if p.parent==project/'data' and p.name.startswith('scan-')),None)
    if campaign is None or crop.suffix.lower()!='.tiff':raise SafetyError('Focus crop has no managed campaign provenance.')
    direct(campaign)
    sources=[crop,crop.with_suffix('.json'),campaign/'scan-request.json',owner_run/'scan-request.json',
             owner_run/'scan-status.json',owner_run/'worker-start.json',owner_run/'STOP']
    for path in sources:direct(path)
    request=read(owner_run/'scan-request.json');status=read(owner_run/'scan-status.json');worker=read(owner_run/'worker-start.json')
    stopped=read(owner_run/'STOP');request_hash=file_hash(owner_run/'scan-request.json')
    if (request.get('kind')!='existing_campaign_storage_continuation_v1'
            or Path(request.get('campaign_output',''))!=campaign
            or Path(status.get('campaign_output',''))!=campaign
            or request.get('parent_request_sha256')!=file_hash(campaign/'scan-request.json')
            or status.get('id')!=owner_run.name or worker.get('id')!=owner_run.name
            or status.get('request_sha256')!=request_hash or worker.get('request_sha256')!=request_hash
            or status.get('hardware_closed') is not True or status.get('closure',{}).get('closed') is not True
            or status.get('state')!='stopped' or status.get('automatic_restart') is not False
            or status.get('pid')!=worker.get('pid') or not worker.get('existing_api_invocation')):
        raise SafetyError('Completed crop lacks exact stopped-owner/campaign ownership and closure.')
    frame=load_frame(crop);capture=frame.metadata.get('zen_vendor_result',{})
    identifier=capture.get('capture_id','');session=capture.get('adapter_session_id','')
    if (not re.fullmatch('[a-f0-9]{32}',identifier) or not re.fullmatch('[a-f0-9]{32}',session)
            or not re.fullmatch('[a-f0-9]{64}',capture.get('raw_rgb_sha256',''))
            or capture.get('job_completed') is not True or capture.get('job_unloaded') is not True):
        raise SafetyError('Focus crop lacks an exact completed and unloaded capture identity.')
    def timestamp(value):
        result=float(value) if isinstance(value,(int,float)) else datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
        if not math.isfinite(result):raise SafetyError('Nonfinite capture/owner time.')
        return result
    started=timestamp(worker['started_at']);finished=timestamp(status['finished_at']);stop=timestamp(stopped['at'])
    if not started<=frame.acquired_at<=stop<=finished:
        raise SafetyError('Completed crop was not acquired during the exact stopped owner interval.')
    shape=capture.get('raw_shape');box=frame.metadata.get('focus_only_roi_source_px')
    if (not isinstance(shape,list) or len(shape)!=3 or any(type(v) is not int for v in shape)
            or shape[2]!=3 or min(shape)<=0 or not isinstance(box,list) or len(box)!=4
            or any(type(v) is not int for v in box)):
        raise SafetyError('Focus crop has invalid full-frame dimensions or source ROI.')
    x0,y0,x1,y1=box
    if (not (0<=x0<x1<=shape[1] and 0<=y0<y1<=shape[0])
            or list(frame.rgb.shape)!=[y1-y0,x1-x0,3] or str(frame.rgb.dtype)!=capture.get('raw_dtype')
            or frame.metadata.get('focus_mask_policy')!='include_chip_edge'
            or not isinstance(frame.metadata.get('physical_stage_position'),dict)):
        raise SafetyError('Saved focus crop differs from its exact parent-frame ROI provenance.')
    folder=direct(root/identifier)
    if Path(capture.get('result_folder',''))!=folder:
        raise SafetyError('Retained vendor folder differs from the completed crop provenance.')
    inventory=[]
    for path in sorted(folder.iterdir()):
        direct(path)
        if not path.is_file() or path.suffix.lower() not in ('.czi','.czjob','.png','.metadata'):
            raise SafetyError('Unexpected retained vendor inventory entry.')
        info=path.stat()
        if info.st_mtime>finished+2:raise SafetyError('Retained vendor file was modified after owner closure.')
        inventory.append({'name':path.name,'bytes':info.st_size,'sha256':file_hash(path)})
    if sum(p['name'].lower().endswith('.czi') and p['bytes']>0 for p in inventory)!=1:
        raise SafetyError('Completed crop review requires exactly one retained nonempty CZI.')
    return {'schema':1,'kind':CROP_REVIEW_KIND,'capture_id':identifier,'capture':capture,
        'crop':crop.relative_to(project).as_posix(),'owner_run':owner_run.relative_to(project).as_posix(),
        'campaign':campaign.relative_to(project).as_posix(),
        'source_files_sha256':{p.relative_to(project).as_posix():file_hash(p) for p in sources},
        'crop_rgb_sha256':hashlib.sha256(np.ascontiguousarray(frame.rgb).tobytes()).hexdigest(),
        'crop_shape':list(frame.rgb.shape),'source_roi_px':box,'inventory':inventory,
        'disposition':'retain_original_vendor_files','raw_acknowledged':False,'scientific_commit':False,
        'placement_evidence':False,'full_frame_duplicate_verified':False,
        'qualification':'Completed/unloaded capture with preserved focus crop and exact stopped-owner provenance. Original full CZI is retained; crop is not a full-frame duplicate or verified focus.'}

def make_stopped_capture_review(root,capture_id,owner_run):
    """Retain one returned bootstrap capture interrupted before raw checkpoint.

    Association uses the exact closed worker, one returned capture counter, and
    matching vendor time/XYZ/optics. No adapter-session receipt survived this
    STOP boundary; explicitly do not assert one or acknowledge/delete the raw.
    """
    from datetime import datetime
    import math
    import xml.etree.ElementTree as ET
    from .vendor_lifecycle import file_hash
    root=direct(root);project=root.parent.parent;owner_run=direct(owner_run)
    if (root!=project/'data'/'zen-captures' or owner_run.parent!=project/'data'
            or not owner_run.name.startswith('scan-') or not re.fullmatch('[a-f0-9]{32}',capture_id)):
        raise SafetyError('Stopped capture review requires canonical managed owner and capture identity.')
    sources=[owner_run/n for n in ('scan-request.json','scan-status.json','worker-start.json','STOP','autonomous-reference/proof.json')]
    for path in sources:direct(path)
    request,status,worker,stop,proof=[read(p) for p in sources]
    request_hash=file_hash(sources[0]);expected_error='STOP requested: '+str(owner_run/'STOP')
    if (status.get('id')!=owner_run.name or worker.get('id')!=owner_run.name
            or status.get('request_sha256')!=request_hash or worker.get('request_sha256')!=request_hash
            or status.get('source_release')!=worker.get('source_release') or status.get('source_release')!=request.get('source_release')
            or status.get('pid')!=worker.get('pid') or status.get('hardware_closed') is not True
            or status.get('state')!='stopped' or status.get('automatic_restart') is not False
            or status.get('stop_errors')!=[] or status.get('watchdog_reason') is not None
            or status.get('failed_phase')!='autonomous_reference' or status.get('error_type')!='Cancelled'
            or status.get('error')!=expected_error or proof.get('state')!='failed' or proof.get('error')!=expected_error
            or proof.get('observations')!=[] or proof.get('budget',{}).get('captures_started')!=1
            or proof.get('budget',{}).get('completed_actions')!=1 or proof.get('xy_travel_um')!=0.):
        raise SafetyError('Stopped capture lacks the exact one-returned-capture/no-motion closed-worker evidence.')
    if not isinstance(request.get('source_release',{}).get('files'),dict):
        raise SafetyError('Stopped capture owner lacks pinned acquisition source.')
    def timestamp(value):
        result=float(value) if isinstance(value,(int,float)) else datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
        if not math.isfinite(result):raise SafetyError('Nonfinite stopped capture time.')
        return result
    started=timestamp(worker['started_at']);finished=timestamp(status['finished_at']);stopped=timestamp(stop['at'])
    if not started<=stopped<=finished:raise SafetyError('Stopped capture owner times are inconsistent.')
    position=proof.get('initial_position_um')
    if position!=status.get('initial_position_um'):raise SafetyError('Stopped bootstrap origin differs from owner.')
    folder=direct(root/capture_id);inventory=[];czis=[]
    for path in sorted(folder.iterdir()):
        direct(path)
        if not path.is_file() or path.suffix.lower() not in ('.czi','.czjob','.png','.metadata'):
            raise SafetyError('Unexpected stopped-capture vendor inventory entry.')
        info=path.stat()
        if info.st_mtime>finished+2:raise SafetyError('Retained capture changed after owner closure.')
        inventory.append({'name':path.name,'bytes':info.st_size,'sha256':file_hash(path)})
        if path.suffix.lower()=='.czi' and info.st_size>0:czis.append(path)
    if len(czis)!=1:raise SafetyError('Stopped capture must retain exactly one nonempty original CZI.')
    czi=czis[0];metadata=direct(czi.with_suffix('.czi.metadata'))
    if metadata.stat().st_size>128*1024:raise SafetyError('Vendor metadata exceeds review bound.')
    properties={p.attrib['key']:p.attrib.get('value','') for p in ET.parse(metadata).getroot().findall('Property')}
    captured=timestamp(properties['File.CreationTime'])
    if not started<=captured<=finished or properties.get('File.Name')!=czi.name or int(properties['File.Size'])!=czi.stat().st_size:
        raise SafetyError('Vendor capture time/name/size does not associate with this stopped owner.')
    def number(value):return float(re.search(r'[-+]?\d+(?:\.\d+)?',value.replace(',','')).group())
    vendor_position={'x':number(properties['Image.StageXPosition']),'y':number(properties['Image.StageYPosition']),
                     'z':number(properties['Image.FocusPosition'])}
    if any(abs(vendor_position[k]-position[k])>.25 for k in ('x','y','z')):
        raise SafetyError('Vendor capture XYZ differs from the stationary stopped bootstrap origin.')
    settings=request['settings']
    if (abs(number(properties['Detector.ExposureTime'])-settings['exposure_ms'])>.02
            or abs(number(properties['Image.Channel.LightSourceIntensity'])-settings['light_percent'])>1.
            or number(properties['Instrument.ObjectiveMagnification'])!=20.):
        raise SafetyError('Vendor capture optical profile differs from the stopped worker request.')
    return {'schema':1,'kind':STOPPED_CAPTURE_REVIEW_KIND,'capture_id':capture_id,
        'owner_run':owner_run.relative_to(project).as_posix(),
        'source_files_sha256':{p.relative_to(project).as_posix():file_hash(p) for p in sources},
        'inventory':inventory,'vendor_position_um':vendor_position,'vendor_capture_time':properties['File.CreationTime'],
        'association':'Exact stopped worker with one returned capture, no motion, and matching vendor time/XYZ/exposure/light/20x profile.',
        'adapter_session_id':None,'completed_unloaded_adapter_receipt_available':False,
        'return_evidence':'Source-bound bootstrap budget records one completed capture action before cooperative STOP.',
        'disposition':'retain_original_vendor_files','raw_acknowledged':False,'scientific_commit':False,
        'placement_evidence':False,'full_frame_duplicate_verified':False,
        'qualification':'Reviewed retained original export only. No persisted frame or adapter-session receipt survived; no raw acknowledgement, focus verification or scientific acceptance.'}


def make_campaign_stop_review(root,capture_id,owner_run):
    """Retain an export interrupted at a reviewed campaign shutdown.

    This is storage reconciliation only. The original CZI remains authoritative;
    an absent frame receipt is not reconstructed or treated as science data.
    """
    import math
    from datetime import datetime
    import xml.etree.ElementTree as ET
    from .vendor_lifecycle import file_hash
    root=direct(root);project=root.parent.parent;owner_run=direct(owner_run)
    if (root!=project/'data'/'zen-captures' or owner_run.parent!=project/'data'
            or not owner_run.name.startswith('scan-') or not re.fullmatch('[a-f0-9]{32}',capture_id)):
        raise SafetyError('Campaign stop review requires managed capture and owner paths.')
    paths=[owner_run/name for name in ('scan-request.json','worker-start.json','scan-status.json','STOP','batch.json')]
    for path in paths:direct(path)
    request,worker,status,stop,batch=[read(path) for path in paths]
    digest=file_hash(paths[0])
    source=request.get('source_release')
    source_files=source.get('files') if isinstance(source,dict) else None
    if not isinstance(source_files,dict) or not source_files:
        raise SafetyError('Campaign export owner lacks pinned acquisition source.')
    if (worker.get('id')!=owner_run.name or status.get('id')!=owner_run.name
            or worker.get('request_sha256')!=digest or status.get('request_sha256')!=digest
            or worker.get('pid')!=status.get('pid') or status.get('hardware_closed') is not True
            or status.get('state')!='stopped' or status.get('stop_errors') not in (None,[])
            or status.get('source_release')!=request.get('source_release')
            or worker.get('source_release')!=request.get('source_release')
            or batch.get('state')!='stopped' or batch.get('error')!='STOP requested: '+str(owner_run/'STOP')):
        raise SafetyError('Campaign export requires the exact cooperatively stopped and closed owner.')
    def timestamp(value):
        result=float(value) if isinstance(value,(int,float)) else datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
        if not math.isfinite(result):raise SafetyError('Invalid export reconciliation timestamp.')
        return result
    started,stopped,finished=timestamp(worker['started_at']),timestamp(stop['at']),timestamp(status['finished_at'])
    if not started<=stopped<=finished:raise SafetyError('Campaign shutdown times are inconsistent.')
    folder=direct(root/capture_id);inventory=[];czis=[]
    for path in sorted(folder.iterdir()):
        direct(path)
        if not path.is_file() or path.suffix.lower() not in ('.czi','.czjob','.png','.metadata'):
            raise SafetyError('Unexpected retained campaign export file.')
        info=path.stat()
        if info.st_mtime>finished+2:raise SafetyError('Export changed after closed owner.')
        inventory.append({'name':path.name,'bytes':info.st_size,'sha256':file_hash(path)})
        if path.suffix.lower()=='.czi' and info.st_size>0:czis.append(path)
    if len(czis)!=1:raise SafetyError('Expected one retained original campaign CZI.')
    czi=czis[0];metadata=direct(czi.with_suffix('.czi.metadata'))
    if metadata.stat().st_size>128*1024:raise SafetyError('Oversized vendor metadata.')
    props={item.attrib['key']:item.attrib.get('value','') for item in ET.parse(metadata).getroot().findall('Property')}
    captured=timestamp(props['File.CreationTime'])
    if (not max(started,stopped-5)<=captured<=finished or props.get('File.Name')!=czi.name
            or int(props['File.Size'])!=czi.stat().st_size):
        raise SafetyError('Retained export does not match the reviewed shutdown interval.')
    def number(value):return float(re.search(r'[-+]?\d+(?:\.\d+)?',value.replace(',','')).group())
    xyz=[number(props[key]) for key in ('Image.StageXPosition','Image.StageYPosition','Image.FocusPosition')]
    settings=request['settings'];bounds=batch['search_bounds_um']
    if (not all(math.isfinite(v) for v in xyz) or not bounds[0]<=xyz[0]<=bounds[1]
            or not bounds[2]<=xyz[1]<=bounds[3]
            or abs(number(props['Detector.ExposureTime'])-batch['reference_context']['camera_resources']['ff_exposure_ms'])>.02
            or abs(number(props['Image.Channel.LightSourceIntensity'])-settings['light_percent'])>1
            or number(props['Instrument.ObjectiveMagnification'])!=20):
        raise SafetyError('Retained export position/optics differ from the stopped campaign.')
    return {'schema':1,'kind':CAMPAIGN_STOP_REVIEW_KIND,'capture_id':capture_id,
        'owner_run':owner_run.relative_to(project).as_posix(),
        'source_files_sha256':{path.relative_to(project).as_posix():file_hash(path) for path in paths},
        'inventory':inventory,'vendor_position_um':xyz,'vendor_capture_time':props['File.CreationTime'],
        'association':'Export at the exact closed campaign STOP interval, inside its envelope and matching its optics.',
        'adapter_session_id':None,'completed_unloaded_adapter_receipt_available':False,
        'disposition':'retain_original_vendor_files','raw_acknowledged':False,'scientific_commit':False,
        'placement_evidence':False,'full_frame_duplicate_verified':False,
        'qualification':'Reviewed retained export only; not a recovered frame receipt, focus measurement or scientific acceptance.'}


def validated_reviews(root):
    """Only valid, exactly matching retained-review receipts reduce pending count."""
    root=Path(root);reviews=root.parent/'vendor-retained-reviews'
    if not reviews.exists():return {}
    direct(reviews);result={}
    for path in sorted(reviews.iterdir()):
        direct(path)
        if not path.is_file() or not re.fullmatch('[a-f0-9]{32}\\.json',path.name):
            raise SafetyError('Unknown vendor retention review entry.')
        if path.stat().st_size>2*1024**2:raise SafetyError('Vendor review exceeds its bound.')
        value=read(path)
        if value.get('capture_id')!=path.stem:raise SafetyError('Retention review identity differs.')
        if value.get('kind')==CAMPAIGN_STOP_REVIEW_KIND:
            owner=root.parent.parent/value.get('owner_run','')
            if any(r.get('kind')==CAMPAIGN_STOP_REVIEW_KIND and r.get('owner_run')==value.get('owner_run') for r in result.values()):
                raise SafetyError('Multiple exports claim one reviewed campaign shutdown.')
            if make_campaign_stop_review(root,path.stem,owner)!=value:
                raise SafetyError('Reviewed campaign shutdown evidence changed.')
        elif value.get('kind')==STOPPED_CAPTURE_REVIEW_KIND:
            owner=root.parent.parent/value.get('owner_run','')
            if any(r.get('kind')==STOPPED_CAPTURE_REVIEW_KIND and r.get('owner_run')==value.get('owner_run') for r in result.values()):
                raise SafetyError('Two retained vendor jobs claim the same one-capture stopped owner.')
            if make_stopped_capture_review(root,path.stem,owner)!=value:
                raise SafetyError('Reviewed stopped-capture evidence changed.')
        elif value.get('kind')==CROP_REVIEW_KIND:
            crop=root.parent.parent/value.get('crop','')
            owner=root.parent.parent/value.get('owner_run','')
            if make_crop_review(root,crop,owner)!=value:
                raise SafetyError('Reviewed completed crop or retained vendor evidence changed.')
        else:
            observation=(root.parent.parent/value.get('observation','')).resolve(strict=True)
            if not observation.is_relative_to(root.parent.parent/'data'):
                raise SafetyError('Review observation escapes managed scientific data.')
            if make_review(root,observation)!=value:raise SafetyError('Reviewed vendor evidence changed or disposition is unproved.')
        result[path.stem]=value
    return result
