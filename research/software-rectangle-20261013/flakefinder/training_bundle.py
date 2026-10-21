"""Portable, human-supervised examples with explicit source/group provenance.

No hardware. Machine dispositions never become ground-truth labels. JPEGs are
appearance inputs only; quantitative contrast remains tied to original TIFFs.
"""
from collections import Counter
from hashlib import sha256
from pathlib import Path
import argparse
import json
import math
import os


def grouped_split(groups, seed='flakefinder-v1'):
    unique = sorted(set(groups), key=lambda g: sha256((seed + '/' + g).encode()).digest())
    if len(unique) < 3:
        return {g: 'train' for g in unique}
    held = max(1, round(len(unique) * .15))
    return {g: 'test' if i < held else 'validation' if i < 2*held else 'train'
            for i, g in enumerate(unique)}


def _numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def numeric_features(row):
    values = {}
    for key in ('area_um2', 'usable_width_um'):
        if _numeric(row.get(key)):
            values[key] = float(row[key])
    contrast = row.get('contrast_rgb')
    if isinstance(contrast, list) and len(contrast) == 3:
        for channel, value in zip('RGB', contrast):
            if _numeric(value):
                values['contrast_' + channel] = float(value)
    # Metrics are measured features only; rank, disposition, sieve flags and
    # thickness predictions are deliberately excluded to prevent target leakage.
    for key, value in row.get('metrics', {}).items():
        if _numeric(value):
            values['metric_' + key] = float(value)
    return values


def examples(catalog, review_export):
    if review_export.get('warnings'):
        raise ValueError('Resolve review-history conflicts before exporting training data')
    fields, rows = {}, []
    for human in review_export.get('regions', []):
        label = human.get('labels', {}).get('semantic', 'unreviewed')
        if label in ('unreviewed', 'uncertain') or human.get('hidden'):
            continue
        if human.get('label_origin') != 'human_review':
            raise ValueError('Only explicit human reviews are training labels')
        source = human['source_identity']; key = source['field_key']
        if key not in fields:
            fields[key] = catalog.get_field(key)
        field = fields[key]
        if source != field['source_identity']:
            raise ValueError('Stale source identity: ' + key)
        machine = next((r for r in field.get('rows', []) if r.get('id') == human['region_id']), {})
        snapshot = human.get('source_proposal') or {}
        if not human.get('is_human_region'):
            loops = machine.get('polygons_px')
            if loops is None:
                loops = ([machine['polygon_px']] if machine.get('polygon_px') else []) + machine.get('holes_px', [])
            current_fingerprint = sha256(json.dumps(dict(id=machine.get('id'), loops=loops,
                bbox=machine.get('bbox_px'), mask_sha256=machine.get('mask_sha256')),sort_keys=True).encode()).hexdigest()
            if snapshot.get('proposal_fingerprint') != current_fingerprint:
                raise ValueError('Reviewed proposal geometry changed or lacks its snapshot: ' + human['region_id'])
        geometry = human.get('human_geometry') or snapshot.get('polygons_px') or machine.get('polygons_px')
        if not geometry and machine.get('polygon_px'):
            geometry = [machine['polygon_px']]
        if not geometry:
            raise ValueError('Missing region geometry: ' + human['region_id'])
        # Corrected masks invalidate old feature measurements. They must be
        # remeasured before numerical training, not silently reused.
        features = {} if human.get('human_geometry') else numeric_features(machine)
        rows.append(dict(region_id=human['region_id'], labels=human['labels'],
            source_identity=source, group_id=source['group_id'], polygons_px=geometry,
            raw_rel=field['raw_rel'], preview_rel=field['preview_rel'],
            features=features, feature_remeasurement_required=bool(human.get('human_geometry')),
            review_revision=human['revision'], review_head_sha256=human['head_sha256'],
            source_proposal=snapshot,
            label_origin='human_review', analysis_record_rel=field.get('analysis_record_rel'),
            material_context=field.get('metadata', {}).get('metadata', {})))
    splits = grouped_split(r['group_id'] for r in rows)
    for row in rows:
        row['split'] = splits[row['group_id']]
    return rows


def export_bundle(catalog, review_export, output, *, include_previews=False):
    rows = examples(catalog, review_export)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    files = {}
    if include_previews:
        (output / 'images').mkdir()
        for row in rows:
            rel = row['preview_rel']
            if rel not in files:
                raw = catalog.resolve(rel).read_bytes()
                digest = sha256(raw).hexdigest()
                target = output / 'images' / (digest + '.jpg')
                if not target.exists():
                    target.write_bytes(raw)
                files[rel] = dict(path=target.relative_to(output).as_posix(), sha256=digest, bytes=len(raw))
            row['appearance_image'] = files[rel]
    payload = ''.join(json.dumps(row, sort_keys=True, allow_nan=False) + '\n' for row in rows).encode()
    (output / 'examples.jsonl').write_bytes(payload)
    summary = dict(schema_version=1, dataset_id=catalog.dataset_id,
        examples=len(rows), human_labels=dict(Counter(r['labels']['semantic'] for r in rows)),
        groups=len(set(r['group_id'] for r in rows)), splits=dict(Counter(r['split'] for r in rows)),
        examples_sha256=sha256(payload).hexdigest(), preview_files=len(files),
        source_kind='human_review_only', no_hardware=True,
        validation_ready=all(any(r['split']==s for r in rows) for s in ('train','validation','test')),
        qualification='JPEGs support appearance learning only. Recompute quantitative features from source TIFFs when geometry changes. Splits hold out whole acquisition chips; validate again on independently prepared chips.')
    (output / 'manifest.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary


def train_cpu(bundle, output):
    """Transparent logistic appearance sieve; never predicts physical thickness."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import classification_report, confusion_matrix
    from sklearn.preprocessing import StandardScaler
    bundle = Path(bundle)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    raw = (bundle / 'examples.jsonl').read_bytes()
    if sha256(raw).hexdigest() != manifest['examples_sha256']:
        raise ValueError('Training manifest checksum mismatch')
    rows = [json.loads(line) for line in raw.splitlines() if line]
    train = [r for r in rows if r['split']=='train' and r['features']]
    validation = [r for r in rows if r['split']=='validation' and r['features']]
    test = [r for r in rows if r['split']=='test' and r['features']]
    if not all((train, validation, test)):
        raise ValueError('Need independently labeled train, validation and test chips; no model trained')
    group_sets = [set(r['group_id'] for r in part) for part in (train, validation, test)]
    if any(group_sets[i] & group_sets[j] for i in range(3) for j in range(i)):
        raise ValueError('Group leakage across training partitions')
    classes = sorted(set(r['labels']['semantic'] for r in train))
    if len(classes)<2 or any(sum(r['labels']['semantic']==c for r in train)<5 for c in classes):
        raise ValueError('Need at least five explicit training examples per class and two classes')
    names = sorted(set.intersection(*(set(r['features']) for r in train)))
    if not names:
        raise ValueError('No common measured numerical features')
    if any(not set(names).issubset(r['features']) for r in validation + test):
        raise ValueError('Validation features need remeasurement before training')
    matrix = lambda part: np.array([[r['features'][n] for n in names] for r in part], dtype=float)
    y = lambda part: [r['labels']['semantic'] for r in part]
    scaler = StandardScaler().fit(matrix(train))
    model = LogisticRegression(class_weight='balanced', max_iter=2000, random_state=0).fit(scaler.transform(matrix(train)), y(train))
    reports = {}
    for name, part in [('validation', validation), ('test', test)]:
        prediction = model.predict(scaler.transform(matrix(part)))
        reports[name] = dict(report=classification_report(y(part),prediction,zero_division=0,output_dict=True),
            confusion_labels=sorted(set(y(part)) | set(model.classes_)),
            confusion_matrix=confusion_matrix(y(part),prediction,labels=sorted(set(y(part)) | set(model.classes_))).tolist(),
            groups=sorted(set(r['group_id'] for r in part)), examples=len(part))
    result = dict(schema_version=1, kind='human_appearance_logistic_sieve', features=names,
        mean=scaler.mean_.tolist(), scale=scaler.scale_.tolist(), classes=model.classes_.tolist(),
        coefficients=model.coef_.tolist(), intercept=model.intercept_.tolist(),
        training_examples_sha256=manifest['examples_sha256'], reports=reports,
        hardware_validated=False, chemical_identity_confirmed=False, thickness_assignment_allowed=False)
    target = Path(output)
    with target.open('x', encoding='utf-8') as stream:
        json.dump(result,stream,indent=2,allow_nan=False)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export'); export.add_argument('--project-root',required=True)
    export.add_argument('--campaign',default='data/whole-chip-standardized-01'); export.add_argument('--output',required=True)
    export.add_argument('--include-previews',action='store_true')
    train = sub.add_parser('train'); train.add_argument('--bundle',required=True); train.add_argument('--output',required=True)
    args = p.parse_args()
    if args.command=='export':
        from .project_catalog import ProjectCatalog
        from .suite_labels import ReviewEventStore
        from .storage import resolve_output_path
        catalog = ProjectCatalog(args.project_root,args.campaign)
        output = resolve_output_path(args.output)
        report = export_bundle(catalog,ReviewEventStore(catalog.root/'annotations'/catalog.dataset_id).export(),output,include_previews=args.include_previews)
    else:
        report = train_cpu(args.bundle,args.output)
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
