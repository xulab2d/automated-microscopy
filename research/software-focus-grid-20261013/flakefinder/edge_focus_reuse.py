"""Typed, hash-pinned focus evidence at a registered edge ROI on this chip."""
from pathlib import Path
import json
import numpy as np
import cv2
from .chip_discovery import file_hash
from .imaging import load_frame
from .model import CameraTransform,Position,SafetyError

_KIND='verified_edge_roi'

def _read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def _context(frame):
    return {key:frame.metadata.get(key) for key in ('camera_job_template','camera_resources','objective_position','illumination_percent')}

def discovery_edge_focus_entries(discovery_folder):
    """Pin completed ROI focus observations from exactly one discovery folder."""
    root=Path(discovery_folder).resolve();source=root/'discovery.json';discovery=_read(source)
    entries=[]
    for index,attempt in enumerate(discovery.get('edge_focus_attempts',[])):
        if attempt.get('state')!='focused' or attempt.get('result',{}).get('success') is not True:continue
        folder=(root/attempt['folder']).resolve()
        if not folder.is_relative_to(root):raise SafetyError('Edge focus evidence escapes its chip discovery.')
        paths={'discovery':source,'result':folder/'focus-result.json','frame':folder/'verified_focus.tiff',
               'metadata':folder/'verified_focus.json','selection':folder/'selection'/'selection.json'}
        if any(not path.is_file() for path in paths.values()):raise SafetyError('Completed edge focus lacks its saved verified ROI evidence.')
        hashes={key+'_sha256':file_hash(path) for key,path in paths.items()}
        if hashes['result_sha256']!=attempt.get('result_sha256'):raise SafetyError('Completed edge focus result hash changed.')
        entries.append({'kind':_KIND,'purpose':'verified','attempt_index':index,
                        **{k:str(v) for k,v in paths.items()},**hashes})
    return entries


def load_edge_focus_entry(entry,plan,*,limits=None,position_tolerance_um=.5):
    """Use ROI XYZ as measured; physical stage centre is a separate assertion."""
    if entry.get('kind')!=_KIND or entry.get('purpose','verified')!='verified':raise SafetyError('Invalid edge ROI focus evidence kind/purpose.')
    keys=('discovery','result','frame','metadata','selection')
    paths={key:Path(entry[key]).resolve() for key in keys}
    for key,path in paths.items():
        if file_hash(path)!=entry.get(key+'_sha256'):raise SafetyError('Verified edge ROI evidence changed: '+key)
    source=Path(plan['discovery_folder']).resolve()/'discovery.json'
    if paths['discovery']!=source or entry['discovery_sha256']!=plan['discovery_sha256']:
        raise SafetyError('Edge ROI evidence belongs to another chip discovery.')
    discovery=_read(source);attempt=discovery['edge_focus_attempts'][entry['attempt_index']]
    folder=(source.parent/attempt['folder']).resolve()
    expected={'result':folder/'focus-result.json','frame':folder/'verified_focus.tiff',
              'metadata':folder/'verified_focus.json','selection':folder/'selection'/'selection.json'}
    if any(paths[k]!=p for k,p in expected.items()) or not folder.is_relative_to(source.parent):
        raise SafetyError('Edge ROI evidence is not the selected discovery attempt.')
    result=_read(paths['result']);selection=_read(paths['selection'])
    if (attempt.get('state')!='focused' or result.get('success') is not True or result.get('outcome')!='resolved_peak'
            or result!=attempt.get('result') or entry['result_sha256']!=attempt.get('result_sha256')):
        raise SafetyError('Edge ROI requires an independently resolved saved focus result.')
    if not np.isfinite([result.get('z_um',np.nan),result.get('score',np.nan)]).all() or result['score']<=0:
        raise SafetyError('Edge ROI focus result must be finite with positive focus signal.')
    confirmations=[s for s in result.get('samples',[]) if 'confirmation' in str(s.get('phase',''))]
    if not any(abs(s.get('z_um',np.inf)-result['z_um'])<=position_tolerance_um for s in confirmations):
        raise SafetyError('Edge ROI focus result lacks its independent confirmation sample.')
    frame=load_frame(paths['frame']);transform=CameraTransform(**plan['transform']);transform.validate_frame(frame)
    if (json.dumps(discovery['transform'],sort_keys=True)!=json.dumps(plan['transform'],sort_keys=True)
            or discovery['source_shape']!=plan['camera_shape'] or _context(frame)!=plan['acquisition_settings']):
        raise SafetyError('Edge ROI camera calibration or optical profile differs.')
    box=frame.metadata.get('focus_only_roi_source_px');h,w=plan['camera_shape'][:2]
    if not isinstance(box,list) or len(box)!=4 or any(type(v) is not int for v in box):raise SafetyError('Edge ROI crop registration is missing.')
    x0,y0,x1,y1=box
    if not (0<=x0<x1<=w and 0<=y0<y1<=h and frame.rgb.shape[:2]==(y1-y0,x1-x0)):
        raise SafetyError('Edge ROI crop geometry differs from the full camera registration.')
    if (frame.metadata.get('focus_mask_policy')!='include_chip_edge' or selection!=attempt.get('selection')
            or frame.metadata.get('edge_focus_selection')!=selection or selection.get('box_px')!=box):
        raise SafetyError('Edge ROI fixed selection differs from its verified focus attempt.')
    physical=frame.metadata.get('physical_stage_position',{})
    physical_xyz=np.asarray([physical.get(a,np.nan) for a in 'xyz'],float)
    point=np.asarray([frame.position.x,frame.position.y,frame.position.z],float)
    registered=physical_xyz[:2]+np.asarray(transform.matrix)@np.array([(x0+x1-w)/2,(y0+y1-h)/2])
    selected=np.asarray(selection.get('point_xy_um',[]),float)
    if (not np.isfinite(physical_xyz).all() or selected.shape!=(2,)
            or np.linalg.norm(physical_xyz[:2]-attempt['requested_xy_um'])>position_tolerance_um
            or np.linalg.norm(registered-point[:2])>position_tolerance_um
            or np.linalg.norm(selected-point[:2])>position_tolerance_um):
        raise SafetyError('Edge ROI measured XY disagrees with physical stage/crop registration.')
    if abs(point[2]-result['z_um'])>position_tolerance_um or abs(physical_xyz[2]-point[2])>position_tolerance_um:
        raise SafetyError('Edge ROI measured Z differs from its resolved focus result.')
    if cv2.pointPolygonTest(np.asarray(plan['scan_plan']['polygon_um'],np.float32),tuple(point[:2]),False)<=0:
        raise SafetyError('Edge ROI measured point must lie inside this chip planning polygon.')
    if limits is not None:
        limits.check(frame.position);limits.check(Position(*physical_xyz))
    for key,path in paths.items():
        if file_hash(path)!=entry[key+'_sha256']:raise SafetyError('Edge ROI evidence changed while reading.')
    return point.tolist(),{**entry,'position_um':point.tolist(),'physical_stage_position_um':physical_xyz.tolist(),
        'result_z_um':float(result['z_um']),'result_success':True,'local_focus_verified':True,
        'result_xy_available':True,'xy_source':'registered_fixed_edge_roi_in_verified_crop',
        'roi_source_px':box,'position_tolerance_um':position_tolerance_um,'acquisition_settings':_context(frame)}
