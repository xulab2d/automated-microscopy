from copy import deepcopy
import numpy as np
import pytest
from flakefinder.hbn_region_evidence import compile_region_evidence,attach_region_evidence
from flakefinder.hbn_thin_evidence import score_thin_evidence


def model():
    return {'material':'hBN','model_id':'fixture','contrast_convention':'(substrate-flake)/substrate',
            'thickness_nm':[0,2,10,100,500],
            'contrast_rgb':[[[0,0,0],[.1,-.1,-.2],[.5,.5,.5],[1,1,1],[2,2,2]]]}


def fixture(annotation=None):
    bundle=model();bundle['alternative_models']=[model()]
    bundle['operator_annotations']=[] if annotation is None else [annotation]
    labels=np.zeros((5,5),np.int32);labels[1:4,1:4]=7
    rows=[{'instance_label':7,'contrast_rgb':[.1,-.1,-.2],'contrast_covariance':(np.eye(3)*.0001).tolist()}]
    reduction={'source_pixel_from_analysis_pixel':[[2,0,.5],[0,2,.5]],
               'source_shape_hw':[10,10],'analysis_shape_hw':[5,5]}
    return bundle,labels,rows,reduction


def annotation(**kwargs):
    return {'tile':'tile_001700','point_px':[4,4],'label':'thin_positive',
            'source':'operator clicked thin feature','source_raw_sha256':'raw-hash',**kwargs}


def attach(bundle,labels,rows,reduction,**kwargs):
    return attach_region_evidence(rows,labels,field='chip001/tile_001700',reduction=reduction,
        compiled=compile_region_evidence(bundle),source_hashes={'tiles/tile_001700.tiff':'raw-hash'},**kwargs)


def test_exact_raw_and_source_point_binding_without_old_region_ids_or_preview():
    args=fixture(annotation());original=deepcopy(args)
    rows,report=attach(*args)
    assert rows[0]['hbn_screen']['passes'] and rows[0]['hbn_screen']['contamination_ambiguous']
    assert report['annotations'][0]['analysis_pixel_xy']==[2,2]
    assert report['annotations'][0]['instance_label']==7
    assert 'hbn_screen' not in args[2][0] and np.array_equal(args[1],original[1])


@pytest.mark.parametrize('change,status',[
    ({'source_raw_sha256':'wrong'},'source_raw_hash_unavailable_or_mismatch'),
    ({'point_px':[0,0]},'point_has_no_measured_region'),
    ({'point_px':[20,20]},'source_point_outside_image'),
    ({'point_px':[3.5,4]},'ambiguous_pixel_boundary'),
])
def test_stale_or_unmapped_annotation_never_grants_support(change,status):
    rows,report=attach(*fixture(annotation(**change)))
    assert not rows[0]['hbn_screen']['passes'] and not rows[0]['hbn_screen']['operator_supported']
    assert report['annotations'][0]['status']==status


def test_wrong_tile_ignored_and_bulk_conflict_blocks_supported_flag():
    rows,report=attach(*fixture(annotation(tile='tile_000001')))
    assert not report['annotations'] and not rows[0]['hbn_screen']['operator_supported']
    args=fixture(annotation());args[0]['operator_annotations'].append(annotation(label='bulk'))
    rows,report=attach(*args)
    assert not rows[0]['hbn_screen']['passes'] and not rows[0]['hbn_screen']['thin_supported']
    assert 'conflicting_operator_annotations' in rows[0]['hbn_screen']['reasons']


def test_compiled_snapshots_equal_direct_scores_and_never_rehash_per_region(monkeypatch):
    import flakefinder.hbn_thin_evidence as thin
    bundle,labels,rows,reduction=fixture()
    direct=score_thin_evidence(rows[0]['contrast_rgb'],rows[0]['contrast_covariance'],bundle,
                              alternative_models=bundle['alternative_models'])
    compiled=compile_region_evidence(bundle)
    bundle['contrast_rgb'][0][1]=[99,99,99]
    monkeypatch.setattr(thin,'canonical_hash',lambda _:(_ for _ in ()).throw(AssertionError('Per-region rehash')))
    got,_=attach_region_evidence(rows,labels,field='tile_001700',reduction=reduction,compiled=compiled)
    assert got[0]['hbn_screen']['metrics']==direct['metrics']


def test_missing_raw_hash_does_not_fallback_to_matching_preview():
    args=fixture(annotation(source_preview_sha256='preview'))
    got,report=attach_region_evidence(args[2],args[1],field='tile_001700',reduction=args[3],
        compiled=compile_region_evidence(args[0]),source_preview_sha256='preview')
    assert not got[0]['hbn_screen']['operator_supported']
    assert report['annotations'][0]['status']=='source_raw_hash_unavailable_or_mismatch'


def test_bulk_context_flags_unknown_absolute_fit_but_preserves_operator_and_regions():
    from flakefinder.hbn_region_evidence import apply_bulk_context
    labels=np.zeros((30,40),np.int32);labels[5:15,5:15]=1;labels[5:15,17:22]=2;labels[5:15,30:35]=3
    anchor={'instance_label':1,'hbn_screen':{'passes':False,'thin_supported':False,'reasons':[],
        'metrics':{'target_vs_bulk_log_ratio':-20.,'best_bulk':{'distance2':400.,'conditional_best_nm':110.}}}}
    candidate={'instance_label':2,'hbn_screen':{'passes':True,'thin_supported':True,'operator_supported':True,
        'reasons':[],'metrics':{'target_vs_bulk_log_ratio':20.}}}
    distant={**deepcopy(candidate),'instance_label':3};rows=[anchor,candidate,distant]
    got,report=apply_bulk_context(rows,labels,[.5,.5])
    assert len(got)==3 and report['flagged_region_ids']==[2]
    screen=got[1]['hbn_screen']
    assert not screen['passes'] and not screen['thin_supported'] and screen['operator_supported']
    assert screen['bulk_context_ambiguous'] and not screen['bulk_context']['nearest_context']['absolute_model_match']
    assert screen['bulk_context']['nearest_context']['distance_um']==1.5
    assert got[2]['hbn_screen']['passes'] and rows[1]['hbn_screen']['passes']


def test_context_does_not_use_tiny_fragments_or_propagate_across_regions():
    from flakefinder.hbn_region_evidence import apply_bulk_context
    labels=np.zeros((30,40),np.int32);labels[5:14,5:14]=1;labels[5:15,15:20]=2
    rows=[{'instance_label':1,'hbn_screen':{'reasons':[],'metrics':{'target_vs_bulk_log_ratio':-20.}}},
          {'instance_label':2,'hbn_screen':{'passes':True,'thin_supported':True,'reasons':[],'metrics':{}}}]
    got,report=apply_bulk_context(rows,labels,[.5,.5])
    assert report['anchor_count']==0 and got[1]['hbn_screen']['passes']


def test_operator_thin_positive_cannot_become_bulk_anchor_from_model_preference():
    from flakefinder.hbn_region_evidence import apply_bulk_context
    labels=np.zeros((30,40),np.int32);labels[5:15,5:15]=1;labels[5:15,17:22]=2
    rows=[{'instance_label':1,'hbn_screen':{'operator_supported':True,'reasons':[],
           'metrics':{'target_vs_bulk_log_ratio':-20.}}},
          {'instance_label':2,'hbn_screen':{'passes':True,'thin_supported':True,'reasons':[],'metrics':{}}}]
    got,report=apply_bulk_context(rows,labels,[.5,.5])
    assert report['anchor_count']==0 and got[1]['hbn_screen']['passes']
    rows[0]['hbn_screen']['annotation_bindings']=[{'label':'bulk'}]
    got,report=apply_bulk_context(rows,labels,[.5,.5])
    assert report['anchor_count']==1 and not got[1]['hbn_screen']['passes']


def test_actual_pilot_three_false_shortlist_regions_become_context_ambiguous():
    import json
    from pathlib import Path
    from flakefinder.hbn_region_evidence import apply_bulk_context
    root=Path('G:/Shared drives/Xu Lab - Microscopy and Instruments/Automated Microscopy/analysis/hbn/scan-20261015-223549-98fae566/chip001/hbn-image-pilot-v4')
    if not (root/'ledger.json').exists():pytest.skip('Saved hBN image pilot unavailable')
    ledger=json.loads((root/'ledger.json').read_text())
    for field,ids in [('tile_000600',[959]),('tile_001700',[1850,1864])]:
        path=root/ledger[field]['field_json'];data=json.loads(path.read_text())
        with np.load(path.with_name('masks.npz')) as pack:labels=pack['labels']
        rows,report=apply_bulk_context(data['regions'],labels,data['reduction']['boundary_resolution_um'])
        assert len(rows)==len(data['regions'])
        for row in rows:
            if row['instance_label'] in ids:
                screen=row['hbn_screen']
                assert screen['bulk_context_ambiguous'] and not screen['thin_supported'] and not screen['passes']
