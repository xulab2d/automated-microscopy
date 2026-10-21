"""Attach local optical measurements to saved features without changing them."""
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import shutil
import time

import cv2

from .analysis import tile_inputs
from .material_selection import normalized_tile
from .measure_existing_regions import ExistingRegionOptions,measure_existing_regions
from .model import atomic_json
from .review import refresh_review
from .thin_contrast import THIN_PRIOR,thin_contrast_evidence


def measure_saved_features(source,output,options=None,callback=None):
    """Copy a saved analysis and append measurements, preserving its identity.

    Source masks, geometries, optical material profile, selections, and catalog
    memberships are immutable. This function has no hardware dependencies.
    """
    from .storage import managed_output_path
    source=Path(source).resolve();output=managed_output_path(output)
    if output==source or output.is_relative_to(source):
        raise ValueError('Measurement output must be separate from the source analysis.')
    if output.exists():raise FileExistsError('Refusing to overwrite an existing measurement analysis.')
    options=options or ExistingRegionOptions()
    read=lambda name:json.loads((source/name).read_text(encoding='utf-8'))
    summary=read('analysis.json');payload=read('flakes.json');observations=read('observations.json')
    items={item['key']:item for item in tile_inputs(payload['source_folder'])}
    by_tile={}
    for row in observations['observations']:by_tile.setdefault(row['tile_key'],[]).append(row)
    shutil.copytree(source,output)
    started=time.perf_counter();updates={};reports=[];total=len(summary['tiles'])
    atomic_json(output/'thin-reference-prior.json',THIN_PRIOR)
    try:
        for index,tile in enumerate(summary['tiles'],1):
            item=items[tile['key']]
            frame,contrast,valid=normalized_tile(item,summary.get('options',{}).get('edge_margin_um',12))
            if hashlib.sha256(frame.rgb.tobytes()).hexdigest()!=tile['sha256']:
                raise ValueError('Saved source image identity changed: '+tile['key'])
            mask_path=(source/tile['instance_mask']).resolve()
            if not mask_path.is_relative_to(source):raise ValueError('Mask path escapes source analysis.')
            copied_mask=(output/tile['instance_mask']).resolve()
            if not copied_mask.is_relative_to(output):raise ValueError('Mask path escapes measurement analysis.')
            mask_hash=hashlib.sha256(mask_path.read_bytes()).hexdigest()
            if hashlib.sha256(copied_mask.read_bytes()).hexdigest()!=mask_hash:
                raise ValueError('Copied instance mask differs from source.')
            labels=cv2.imread(str(mask_path),cv2.IMREAD_UNCHANGED)
            if labels is None:raise ValueError('Cannot read exact source instance labels.')
            measurements,report=measure_existing_regions(frame,contrast,labels,valid,options)
            report.update(tile_key=tile['key'],source_sha256=tile['sha256'],instance_file_sha256=mask_hash)
            reports.append(report)
            for row in by_tile.get(tile['key'],[]):
                measurement=measurements.get(int(row['instance_label']),{
                    'measurement_status':'unmeasurable','reason':'missing_source_instance_label','mask_modified':False})
                if measurement['measurement_status']=='measured':
                    reference=thin_contrast_evidence(measurement['contrast_rgb'],measurement['contrast_covariance'])
                else:
                    reference={'status':'not_measured','compatible':None,'reason':measurement.get('reason'),
                               'model_id':THIN_PRIOR['model_id'],'layer_number':None,'chemical_identity_confirmed':False}
                additions={'local_contrast_measurement':measurement,'thin_reference_evidence':reference}
                row.update(additions);updates[row['id']]=additions
            atomic_json(output/'existing-feature-progress.json',{
                'state':'measuring','completed':index,'total':total,'tile_key':tile['key'],
                'elapsed_seconds':time.perf_counter()-started})
            if callback:callback(index,total,tile['key'],report)
        for row in payload['flakes']:
            if row['id'] not in updates:raise ValueError('Catalog identity lacks its source observation: '+row['id'])
            row.update(updates[row['id']])
        measured=sum(row['local_contrast_measurement']['measurement_status']=='measured' for row in observations['observations'])
        compatible=sum(row['thin_reference_evidence'].get('compatible') is True for row in observations['observations'])
        report={'method':'local_contrast_on_immutable_existing_features','source_analysis':str(source),
            'source_dataset_id':payload['dataset_id'],'profile':'thin-reference-prior.json',
            'options':asdict(options),'tiles':total,'observations':len(observations['observations']),
            'measured_observations':measured,'unmeasurable_observations':len(observations['observations'])-measured,
            'literature_direction_compatible_observations':compatible,'catalog_changed':False,
            'material_selection_changed':False,'mask_geometry_changed':False,
            'elapsed_seconds':time.perf_counter()-started,
            'qualification':'Local measurements and nearby-oxide literature direction only. Original catalog and material-color selection remain unchanged; no layer or chemical identity assigned.'}
        summary['existing_feature_contrast']=report
        atomic_json(output/'analysis.json',summary);atomic_json(output/'flakes.json',payload)
        atomic_json(output/'observations.json',observations)
        atomic_json(output/'existing-feature-measurements.json',{'summary':report,'tiles':reports})
        refresh_review(output,source)
        atomic_json(output/'existing-feature-progress.json',{'state':'complete','completed':total,'total':total,
            'elapsed_seconds':time.perf_counter()-started})
        return report
    except Exception as exc:
        atomic_json(output/'existing-feature-progress.json',{'state':'failed','error':str(exc),'completed':len(reports),
            'total':total,'elapsed_seconds':time.perf_counter()-started})
        raise
