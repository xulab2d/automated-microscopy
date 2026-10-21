"""Revalidate saved geometry into a NEW v2 lineage without changing old files."""
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import hashlib,json,time
import numpy as np
from .campaign_restart import read,digest,checked_path
from .chip_discovery import EvidenceMap,file_hash,contained_field_size
from .imaging import load_frame
from .layout import SubstrateReference
from .model import SafetyError,atomic_json
from .photometric_edges import ALGORITHM_VERSION,assess_edge_profile,spatial_bracket_evidence
from .sparse_edges import _label_edge,validated_coherent_edge,fit_validated_outline
from .sparse_acquisition import SparseEdgeOptions
from . import sparse_acquisition,sparse_edges,photometric_edges


def prepare_discovery_import(source,output,reference,transform,limits,profile,*,additional_probes=0):
    """Preserve unused old budget, or explicitly allocate a bounded new supplement.

    All old fields become fingerprint-bound imported observations and therefore
    never overwrite/newly consume an old acquisition counter. Without a supplied
    supplement the new budget is at most the OLD unspent budget. Zero unspent
    budget remains visibly unresolved; it does not starve the whole queue.
    """
    if isinstance(additional_probes,bool) or not isinstance(additional_probes,int) or not 0<=additional_probes<=32:
        raise ValueError('Additional geometric probe allowance must be an explicit integer0..32.')
    source=Path(source).resolve();out=Path(output).resolve();old=read(source/'discovery.json')
    if out.exists() and any(out.iterdir()):raise SafetyError('Use a new empty derived discovery directory.')
    if old.get('state') in ('running','focusing'):raise SafetyError('Do not import active discovery state.')
    if old['transform']!=json.loads(json.dumps(asdict(transform))) or old['reference']['context']!=SubstrateReference.from_frame(reference).context:
        raise SafetyError('Saved discovery transform/optical profile differs.')
    if not assess_edge_profile(profile,reference)['supported']:raise SafetyError('Unsupported restart edge profile.')
    options=SparseEdgeOptions(**old['options']);unspent=max(0,options.max_probes-len(old['records']))
    allowance=min(32,unspent+additional_probes)
    records=[];observations=[];edges=[];evidence=EvidenceMap(options.search_bounds_um,options.cell_um)
    for original in old['records']+old.get('support_records',[]):
        record=json.loads(json.dumps(original));hashes={}
        for name,expected in original['hashes'].items():
            path=checked_path(source,name)
            if file_hash(path)!=expected:raise SafetyError('Saved import evidence changed: '+str(path))
            hashes[str(path)]=expected
        image=checked_path(source,original['image']);meta=read(image.with_suffix('.json'))
        if not assess_edge_profile(profile,SimpleNamespace(metadata=meta['metadata']))['supported']:
            raise SafetyError('Saved import frame has a different optical profile.')
        with np.load(checked_path(source,original['labels_file']))as saved:labels=saved['labels'].copy()
        proposal=_label_edge(labels,original['position_um'],old['source_shape'],transform.matrix,standard_fallback=True)
        edge=None;report={'accepted':False,'reason':'No supported label proposal; raw decoding unnecessary.'}
        if proposal is not None:edge,report=validated_coherent_edge(load_frame(image),labels,transform.matrix,profile)
        record.update(image=str(image),labels_file=str(checked_path(source,original['labels_file'])),hashes=hashes,
            original_edge=record.get('edge'),edge=edge,edge_validation=report,
            spatial_bracket_evidence=spatial_bracket_evidence(labels,original.get('classification','unknown')))
        record.setdefault('classification','unknown');record.setdefault('requested_xy_um',original['position_um'][:2])
        if edge:edge.update(image=str(image),position_um=record['position_um'],phase=record.get('phase','imported'));edges.append(edge)
        observations.append((str(image),labels,record['position_um'][:2]));records.append(record)
        evidence.add(labels,record['position_um'],old['source_shape'],transform)
    fit=fit_validated_outline(edges,old['seed_xy_um'],options.search_bounds_um,options.margin_um,observations,transform.matrix,old['source_shape'])
    context={**old.get('context',{}),'restart_imported_records_sha256':digest(records),'restart_probe_allowance':allowance,
        'restart_placement_status':'historical_unverified'}
    # This is the existing v2 resume payload, not a rewritten old identity.
    options.max_probes=max(1,allowance)
    ref=SubstrateReference.from_frame(reference)
    payload={'schema':2,'algorithm':ALGORITHM_VERSION,'algorithm_source_sha256':file_hash(sparse_acquisition.__file__),
        'edge_model_sha256':file_hash(sparse_edges.__file__),'photometric_model_sha256':file_hash(photometric_edges.__file__),
        'edge_validation_profile':profile,'seed_xy_um':old['seed_xy_um'],'options':asdict(options),'context':context,
        'transform':asdict(transform),'source_shape':list(reference.rgb.shape),'reference':ref.to_dict(),
        'reference_rgb':np.median(reference.rgb.reshape(-1,3),axis=0).tolist(),
        'reference_pixels_sha256':hashlib.sha256(np.ascontiguousarray(reference.rgb).tobytes()).hexdigest(),
        'limits':asdict(limits),'field_rectangle_um':contained_field_size(reference,transform).tolist(),
        'assumption':'One separated convex chip with approximately straight edges. Coarse hops may cross unobserved gaps.',
        'coverage_kind':'estimated_boundary_not_full_interior_acquisition','all_chips_found':False,
        'coverage_requirement':'Validate extent from scientific-raster border images; expand or replan if substrate reaches its perimeter.'}
    state={**payload,'fingerprint':digest(payload),'state':'estimated' if fit['status']=='estimated'else'unresolved',
        'started_at':time.time(),'updated_at':time.time(),'records':[],'imported_records':records,'rays':{},'estimated_outline':fit,
        'lineage':{'source_discovery':str(source/'discovery.json'),'source_discovery_sha256':file_hash(source/'discovery.json'),
            'original_fingerprint':old['fingerprint'],'old_budget':old['options']['max_probes'],'old_used':len(old['records']),
            'old_unspent':unspent,'explicit_additional_allowance':additional_probes,'new_probe_allowance':allowance,
            'old_identity_modified':False,'new_acquisition_allowed':allowance>0}}
    # Successful saved focus only seeds geometric search at this SAME chip.
    focus=old.get('edge_focus_attempts',[])
    if focus and focus[-1].get('state')=='focused':
        state['edge_focus_attempts']=json.loads(json.dumps(focus))
        for item in state['edge_focus_attempts']:
            item['folder']=str(checked_path(source,item['folder']))
            if file_hash(Path(item['folder'])/'focus-result.json')!=item['result_sha256']:raise SafetyError('Imported focus result changed.')
    out.mkdir(parents=True,exist_ok=True);atomic_json(out/'discovery.json',state);atomic_json(out/'estimated-outline.json',fit)
    np.savez_compressed(out/'evidence.npz',flags=evidence.flags,cell_um=evidence.cell,bounds_um=evidence.bounds)
    return state
