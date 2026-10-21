import numpy as np
import pytest
from flakefinder.hbn_thin_evidence import score_thin_evidence


def model(thin=(.1, -.1, -.2), bulk=(1., 1., 1.)):
    return {'material':'hBN', 'model_id':'fixture',
            'contrast_convention':'(substrate-flake)/substrate',
            'thickness_nm':[0, 2, 10, 100, 500],
            'contrast_rgb':[[[0,0,0],list(thin),[.5,.5,.5],list(bulk),[2,2,2]]]}


RGB = [.1, -.1, -.2]
COV = np.eye(3)*.0001


def test_thin_against_distinct_alternative_passes_without_shape():
    got = score_thin_evidence(RGB,COV,model(),alternative_models=[model(thin=[-.5,.5,.5])])
    assert got['passes'] and got['thin_supported'] and got['target_band']=='below_5_nm'
    assert got['thickness_interval_nm']==[2.,2.]


def test_transparent_film_ambiguity_is_not_chemical_identification():
    got = score_thin_evidence(RGB,COV,model(),alternative_models=[model()])
    assert got['thin_supported'] and got['contamination_ambiguous'] and not got['passes']
    positive={'label':'thin_positive','source':'operator actual feature annotation'}
    got = score_thin_evidence(RGB,COV,model(),alternative_models=[model()],operator_annotation=positive)
    assert got['passes'] and got['contamination_ambiguous'] and got['operator_supported']


def test_bulk_alias_cannot_be_bypassed_by_operator_positive():
    got = score_thin_evidence(RGB,COV,model(bulk=RGB),operator_annotation={'label':'thin_positive','source':'operator'})
    assert got['alias_ambiguous'] and not got['passes'] and got['thickness_interval_nm']==[2.,100.]


def test_operator_bulk_overrides_optical_target_and_missing_alternative_is_explicit():
    got = score_thin_evidence(RGB,COV,model(),operator_annotation={'label':'bulk','source':'operator'})
    assert not got['passes'] and got['target_band']=='thick_or_bulk'
    assert not got['thin_supported'] and got['contamination_ambiguous']
    assert not got['contamination_assessed'] and 'transparent_film_alternatives_unassessed' in got['reasons']


def test_profiles_scenarios_individually_and_null_never_passes():
    m=model(thin=[-1,-1,-1]);m['contrast_rgb'].append(model()['contrast_rgb'][0])
    got=score_thin_evidence(RGB,COV,m,operator_annotation={'label':'thin_positive','source':'operator'})
    assert got['passes'] and got['metrics']['best_target']['scenario_index']==1
    got=score_thin_evidence([0,0,0],COV,m,operator_annotation={'label':'thin_positive','source':'operator'})
    assert not got['passes'] and not got['thin_supported']


def test_invalid_measurement_or_unproven_operator_annotation_rejected():
    with pytest.raises(ValueError): score_thin_evidence(RGB,np.zeros((3,3)),model())
    with pytest.raises(ValueError): score_thin_evidence(RGB,COV,model(),operator_annotation={'label':'thin_positive'})


def test_saved_operator_examples_keep_thin_evidence_and_explicit_bulk_override():
    import json
    from pathlib import Path
    root=Path('G:/Shared drives/Xu Lab - Microscopy and Instruments/Automated Microscopy/analysis/hbn/scan-20261015-223549-98fae566/chip001')
    if not (root/'hbn-pilot-v2/ledger.json').exists():
        pytest.skip('Saved immutable hBN pilot unavailable')
    saved_model=json.loads((root/'models/hbn90-conditional-v2.json').read_text())
    ledger=json.loads((root/'hbn-pilot-v2/ledger.json').read_text())
    for field,number,label in [('tile_001200',1189,'thin_positive'),
                               ('tile_001700',307,'thin_positive'),
                               ('tile_001700',546,'bulk')]:
        data=json.loads((root/'hbn-pilot-v2'/ledger[field]['field_json']).read_text())
        region=next(r for r in data['regions'] if r['instance_label']==number)
        got=score_thin_evidence(region['contrast_rgb'],region['contrast_covariance'],saved_model,
            operator_annotation={'label':label,'source':'October16 operator qualitative annotation; no thickness calibration'})
        assert got['passes'] == (label=='thin_positive')
        assert got['target_band']==('below_5_nm' if label=='thin_positive' else 'thick_or_bulk')
        assert not got['contamination_assessed']
        assert got['contamination_ambiguous']
        if label=='bulk': assert not got['thin_supported']
