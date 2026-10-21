"""Prepare one saved raw field for material inference, without hardware or JPEG radiometry."""
from pathlib import Path
import json
import time

import cv2
import numpy as np
from sklearn.covariance import OAS

from .imaging import load_frame
from .model import CameraTransform, Frame
from .material_profiles import CONVENTION, canonical_hash
from .terrace_domain import ChipDomain, digest, normalize_supported


def _read_mask(path, key, shape, *, labels=False):
    path=Path(path)
    if path.suffix.lower()=='.npz':
        with np.load(path,allow_pickle=False) as saved:
            if key not in saved:raise ValueError(f'{path} must contain array {key!r}.')
            value=np.asarray(saved[key])
    else:
        value=cv2.imread(str(path),cv2.IMREAD_UNCHANGED)
    if value is None or value.shape!=shape:raise ValueError(f'{key} must have the exact source image dimensions.')
    if not np.isfinite(value).all():raise ValueError(f'{key} contains nonfinite values.')
    if labels:
        if value.dtype.kind not in 'ui' or np.any(value<0) or value.max()>np.iinfo(np.int32).max:
            raise ValueError('Parent labels must be nonnegative int32-compatible source labels.')
        return value.astype(np.int32)
    if not np.isin(value,[0,1,255]).all():raise ValueError(f'{key} must be binary; use an explicit mask, not semantic class labels.')
    return value!=0


def validate_input_context(context):
    """Unmeasured settings are allowed only when stated, never inferred as calibrated."""
    if context.get('material') not in ('graphene','hBN'):raise ValueError('Context requires material graphene or hBN.')
    if not np.isfinite(context.get('oxide_nm',np.nan)) or context['oxide_nm']<=0:raise ValueError('Context requires positive oxide_nm.')
    if not context.get('optical_profile_id') or str(context['optical_profile_id']).startswith('EDIT_'):
        raise ValueError('Context requires the actual optical_profile_id; edit the example template first.')
    radiometry=context.get('radiometry',{})
    if radiometry.get('raw_linear_status') not in ('verified','assumed_unverified'):
        raise ValueError('Declare radiometry.raw_linear_status as verified or assumed_unverified.')
    if not radiometry.get('response_assumption') or not radiometry.get('dark_offset_status'):
        raise ValueError('Declare response_assumption and dark_offset_status; absent calibration is not zero uncertainty.')
    offset=np.asarray(radiometry.get('dark_offset_rgb'),float)
    if offset.shape!=(3,) or not np.isfinite(offset).all() or np.any(offset<0):
        raise ValueError('Explicit finite nonnegative dark_offset_rgb required, including declared zero when unmeasured.')
    return offset


def prepare_material_input(*, context, tiff=None, domain_mask=None, substrate_mask=None,
                           chip_folder=None, tile_id=None, parent_labels=None,
                           parent_key='labels', edge_margin_um=12., maximum_pixels=16_000_000):
    """Return arrays + provenance for one raw TIFF and independent background support.

    Scanner mode reads the saved manifest geometry and hash-verified substrate
    labels. Explicit-mask mode requires source-sized binary masks; the caller is
    responsible for their independent annotation. Neither mode selects background
    from the desired material color. This function writes nothing.
    """
    started=time.perf_counter();offset=validate_input_context(context)
    if not np.isfinite([edge_margin_um,maximum_pixels]).all() or edge_margin_um<0 or maximum_pixels<1:
        raise ValueError('Nonnegative finite edge margin and positive pixel limit required.')
    if chip_folder is not None:
        if tiff is not None or domain_mask is not None or substrate_mask is not None or not tile_id:
            raise ValueError('Use chip_folder + tile_id, or tiff + domain_mask + substrate_mask.')
        folder=Path(chip_folder).resolve();manifest_path=folder/'manifest.json'
        manifest=json.loads(manifest_path.read_text(encoding='utf-8-sig'))
        if tile_id not in manifest.get('completed',{}):raise ValueError('Tile is not a completed saved field.')
        tiff=folder/'tiles'/(tile_id+'.tiff');record=manifest['completed'][tile_id]
    elif tiff is None or domain_mask is None or substrate_mask is None or tile_id is not None:
        raise ValueError('Explicit raw input requires tiff, domain_mask and substrate_mask.')
    path=Path(tiff).resolve()
    if path.suffix.lower() not in ('.tif','.tiff'):raise ValueError('Use scientific raw TIFF, never JPEG preview.')
    # Inspect before decoding to bound memory; scanner RGB TIFFs contain one page.
    from PIL import Image
    with Image.open(path) as saved:
        if saved.n_frames!=1 or np.prod(saved.size)>maximum_pixels or saved.mode!='RGB':
            raise ValueError('Expected one bounded uint16 RGB raw TIFF, not a preview or stack.')
    frame=load_frame(path);shape=frame.rgb.shape[:2]
    if frame.rgb.dtype!=np.dtype('uint16'):raise ValueError('Expected uint16 raw data, not an 8-bit preview.')
    provenance={'raw_tiff':str(path),'raw_sha256':digest(path),'sidecar':str(path.with_suffix('.json')),
        'sidecar_sha256':digest(path.with_suffix('.json')),'context':context,'context_sha256':canonical_hash(context),
        'contrast_convention':CONVENTION,'source_pixel_size_um':list(frame.pixel_size_um),
        'source_optics':{key:frame.metadata.get(key) for key in ('camera_resources','objective','illumination_percent','camera_job_template')},
        'background_systematic_covariance':'Unquantified; spatial fit validation is not complete calibration uncertainty.'}
    if chip_folder is not None:
        for relative,actual in [('tiles/'+path.name,provenance['raw_sha256']),('tiles/'+path.with_suffix('.json').name,provenance['sidecar_sha256'])]:
            if record.get('hashes',{}).get(relative)!=actual:raise ValueError('Saved raw/sidecar differs from acquisition manifest: '+relative)
        measured=manifest.get('boundary_validation',{}).get('acquired_raster_evidence',{})
        verified=measured.get('status')=='enclosed' and measured.get('outline_um')
        polygon=np.asarray(measured['outline_um'] if verified else manifest['plan']['polygon_um'],float)
        if polygon.ndim!=2 or polygon.shape[1]!=2 or len(polygon)<3 or not np.isfinite(polygon).all():raise ValueError('Invalid saved chip geometry.')
        transform=CameraTransform(**manifest['transform']);transform.validate_frame(frame)
        domain=ChipDomain(folder,manifest,polygon,'observed_enclosed' if verified else 'provisional_plan_unclosed',{})
        valid,support,evidence=domain.masks({'tile_id':tile_id,'transform':transform},frame,edge_margin_um)
        provenance.update(manifest=str(manifest_path),manifest_sha256=digest(manifest_path),domain_evidence=evidence)
    else:
        valid=_read_mask(domain_mask,'valid_mask',shape);support=_read_mask(substrate_mask,'substrate_mask',shape)
        if np.any(support&~valid):raise ValueError('Background samples must be inside the geometric domain.')
        provenance['domain_evidence']={'kind':'caller_supplied_independent_masks','domain_path':str(Path(domain_mask).resolve()),
            'domain_sha256':digest(domain_mask),'substrate_path':str(Path(substrate_mask).resolve()),'substrate_sha256':digest(substrate_mask),
            'qualification':'Annotation provenance is supplied by the operator; not an automatic material-color selection.'}
    white=float(frame.metadata.get('white_level',np.nan))
    if not np.isfinite(white) or white<=0 or white>65535:raise ValueError('Raw sidecar must state sensor white_level.')
    saturated=np.any(frame.rgb>=white,axis=2)
    corrected=frame.rgb.astype(np.float32)-offset.astype(np.float32)
    valid=valid&np.all(corrected>0,axis=2);support=support&valid&~saturated
    corrected_frame=Frame(corrected,frame.position,frame.pixel_size_um,frame.sequence,frame.acquired_at,frame.metadata)
    contrast,normalization=normalize_supported(corrected_frame,valid,support)
    samples=contrast[support];samples=samples[::max(1,int(np.ceil(len(samples)/20000)))]
    noise=OAS().fit(samples).covariance_+np.eye(3)*1e-12
    arrays={'contrast_rgb':contrast,'valid_mask':valid,'substrate_mask':support,'noise_covariance':noise,
        'pixel_size_um':np.asarray(frame.pixel_size_um),'saturated_mask':saturated}
    if parent_labels is not None:
        arrays['parent_labels']=_read_mask(parent_labels,parent_key,shape,labels=True)
        provenance['parent_labels']={'path':str(Path(parent_labels).resolve()),'key':parent_key,'sha256':digest(parent_labels),'preserved':True}
    provenance.update(normalization=normalization,raw_noise_method='OAS covariance of independently supported raw-contrast pixels; not divided by pixel count',
        raw_noise_samples=len(samples),valid_pixels=int(valid.sum()),substrate_pixels=int(support.sum()),seconds=time.perf_counter()-started,
        calibration_required=context['radiometry']['raw_linear_status']!='verified' or context['radiometry']['dark_offset_status']!='measured',
        layer_assignment_allowed=False,chemical_identity_confirmed=False)
    return arrays,provenance
