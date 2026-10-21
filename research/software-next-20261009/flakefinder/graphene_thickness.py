"""Provisional equivalent graphene layers from uncalibrated spectral scenarios.

No acquisition imports. Scenario envelopes are sensitivity ranges, not confidence
intervals. Absolute RGB mismatch is retained; the observed bands are never used
as labeled thickness standards or to fit channel gains.
"""
from pathlib import Path
import hashlib
import json
import re
import threading
import numpy as np


def estimate_thickness(contrast_rgb, contrast_covariance, ensemble):
    x=np.asarray(contrast_rgb,float);cov=np.asarray(contrast_covariance,float)
    curves=np.asarray(ensemble['contrast_rgb'],float)
    layers=np.asarray(ensemble['thickness_nm'],float)/float(ensemble.get('layer_spacing_nm',.34))
    if x.shape!=(3,) or cov.shape!=(3,3) or not np.isfinite(x).all() or not np.isfinite(cov).all():
        raise ValueError('Finite RGB and 3x3 covariance required.')
    if not np.allclose(cov,cov.T) or np.linalg.eigvalsh(cov).min() < -1e-12:
        raise ValueError('Measurement covariance must be positive semidefinite.')
    if curves.ndim!=3 or curves.shape[1:]!=(len(layers),3) or not np.isfinite(curves).all() or np.any(np.diff(layers)<=0):
        raise ValueError('Finite increasing thickness scenario grid required.')
    allowance=float(ensemble['engineering_rgb_sigma'])
    if not np.isfinite(allowance) or allowance<=0:raise ValueError('Positive explicit model allowance required.')
    residual=x-curves;inverse=np.linalg.inv(cov+np.eye(3)*allowance**2)
    distance=np.einsum('stc,cd,std->st',residual,inverse,residual)
    scenario,index=np.unravel_index(np.argmin(distance),distance.shape)
    bests=np.argmin(distance,axis=1);threshold=float(ensemble['absolute_match_distance2'])
    accepted=np.any(distance<=threshold,axis=0)
    positions=np.flatnonzero(accepted);intervals=[]
    for group in np.split(positions,np.flatnonzero(np.diff(positions)>1)+1):
        if len(group):intervals.append([float(layers[group[0]]),float(layers[group[-1]])])
    best=float(layers[index]);mismatch=bool(distance[scenario,index]>threshold)
    boundary=index in (0,len(layers)-1)
    status='model_mismatch' if mismatch else 'grid_boundary' if boundary else 'conditional_optical_fit'
    provenance=ensemble.get('scenario_provenance',[])
    linear={}
    linear_indices=[i for i,p in enumerate(provenance) if p.get('gamma')==1.]
    if linear_indices:
        linear_distance=distance[linear_indices];ls,li=np.unravel_index(np.argmin(linear_distance),linear_distance.shape)
        linear={'linear_response_layer_estimate':float(layers[li]),
            'linear_response_best_distance2':float(linear_distance[ls,li]),
            'linear_response_predicted_rgb':curves[linear_indices[ls],li].tolist(),
            'linear_response_scenario_best_layer_range':[float(layers[bests[linear_indices]].min()),float(layers[bests[linear_indices]].max())]}
    return {'status':status,'provisional_layer_estimate':best,'equivalent_thickness_nm':float(ensemble['thickness_nm'][index]),
        'best_scenario':provenance[scenario] if provenance else None,**linear,
        'scenario_best_layer_range':[float(layers[bests].min()),float(layers[bests].max())],
        'compatible_layer_intervals':intervals,'best_scenario_index':int(scenario),
        'best_distance2':float(distance[scenario,index]),'rgb_residual':residual[scenario,index].tolist(),
        'predicted_rgb':curves[scenario,index].tolist(),'model_mismatch':mismatch,'grid_boundary':boundary,
        'grid_step_layers':float(np.max(np.diff(layers))),'relative_red_contrast':float(x[0]),
        'range_kind':'finite scenario sensitivity, not a confidence interval',
        'layer_assignment_confirmed':False,'chemical_identity_confirmed':False,
        'qualification':'Conditional equivalent layers under assumed graphene optics. Nearest fit is diagnostic when model_mismatch; no calibrated layer identification.'}


def _read_hash(path,expected):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Thickness evidence checksum mismatch: '+Path(path).name)
    return json.loads(raw)


def _inside(root,relative):
    p=Path(relative)
    if p.is_absolute() or p.drive or '..' in p.parts:raise ValueError('Thickness path must be relative and confined.')
    result=(root/p).resolve()
    if not result.is_relative_to(root.resolve()):raise ValueError('Thickness evidence path escaped root.')
    return result


def load_thickness_sidecar(path,expected_sha256,source_plan_sha256):
    value=_read_hash(path,expected_sha256)
    if value.get('schema')!=1 or value.get('source_plan_sha256')!=source_plan_sha256:
        raise ValueError('Thickness sidecar differs from selected analysis.')
    return value


class ThicknessReview:
    """Lazy detail-only estimates; reads one pinned field, never images or masks."""
    def __init__(self,project_root):
        self.root=Path(project_root).resolve();self._cache=None;self._lock=threading.RLock()

    def data(self,run,observation):
        if not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',run) or not re.fullmatch(r'chip\d+/tile_\d{6}/\d+',observation):
            raise ValueError('Exact run and observation required.')
        with self._lock:
            folder=_inside(self.root,'data/'+run);chip=observation.split('/')[0]
            selector_path=folder/('graphene-thickness-selection-'+chip+'.json')
            if not selector_path.is_file():return {'status':'not_available','observation':observation,
                'reason':'No thickness estimate has been generated for this selected analysis.'}
            selector=json.loads(selector_path.read_bytes())
            selected=folder/('graphene-analysis-selection-'+chip+'.json')
            if not selected.is_file():selected=folder/'graphene-analysis-selection.json'
            analysis=json.loads(selected.read_bytes())
            if selector.get('run_id')!=run or selector.get('source_plan_sha256')!=analysis.get('plan_sha256'):
                raise ValueError('Thickness selection differs from current analysis.')
            output=_inside(self.root,analysis['analysis_relative_path'])
            plan=_read_hash(output/'plan.json',analysis['plan_sha256'])
            sidecar=_inside(self.root,selector['path']);stat=sidecar.stat()
            key=(str(sidecar),stat.st_size,stat.st_mtime_ns,selector['sha256'])
            if self._cache is None or self._cache[0]!=key:
                self._cache=(key,load_thickness_sidecar(sidecar,selector['sha256'],analysis['plan_sha256']))
            bundle=self._cache[1]
            if bundle.get('run_id')!=run or bundle.get('chip')!=chip or bundle.get('source_model_sha256')!=plan['model_sha256']:
                raise ValueError('Thickness evidence source identity mismatch.')
            field,label=observation.rsplit('/',1);source=bundle['source_fields'].get(field)
            if not source:return {'status':'unassessed_field','observation':observation,
                'reason':'This field has no assessed, pinned contrast measurements.'}
            payload=_read_hash(_inside(output,source['path']),source['sha256'])
            if payload.get('field')!=field:raise ValueError('Thickness source field identity mismatch.')
            row=next((r for r in payload.get('regions',[]) if str(r['instance_label'])==label),None)
            if row is None:raise ValueError('Observation absent from pinned thickness source.')
            estimate=bundle['regions'].get(observation)
            if estimate is None:
                estimate=estimate_thickness(row['contrast_rgb'],row['contrast_covariance'],bundle['ensemble'])
                if not row.get('measurement',{}).get('interior_measurement_available'):
                    estimate['status']='no_resolved_interior'
                    estimate['qualification']+=' Whole-region colour only; unresolved interior.'
            return {**estimate,'observation':observation,'source_plan_sha256':bundle['source_plan_sha256'],
                'source_field_sha256':source['sha256'],'thickness_sidecar_sha256':selector['sha256'],
                'assumptions':bundle['assumptions'],'relative_order_qualification':'Red contrast order only within this optical context; not an absolute layer standard.'}
