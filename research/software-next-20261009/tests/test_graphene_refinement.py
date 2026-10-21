import numpy as np
import cv2
import pytest
from flakefinder.graphene_refinement import refine_candidate


def fixture():
    rng=np.random.default_rng(9);truth=np.zeros((180,180),bool);truth[55:125,45:135]=True
    truth[55:85,105:135]=False # true concavity
    truth[92:106,75:89]=False # physical background hole
    image=np.ones((180,180,3),np.float32)*2000;image[truth]*=[.95,.93,.96]
    image+=rng.normal(0,2,image.shape)
    valid=np.ones(truth.shape,bool);background=~cv2.dilate(np.uint8(truth),np.ones((9,9),np.uint8)).astype(bool)
    return rng,truth,image,valid,background


def test_refinement_improves_noisy_mask_without_filling_real_hole_or_concavity():
    rng,truth,image,valid,bg=fixture();seed=truth.copy();seed[rng.random(seed.shape)<.12]=False
    before=seed.copy();result=refine_candidate(image,seed,valid,[.2,.2],substrate_mask=bg)
    refined=result['refined_mask']
    assert np.array_equal(seed,before) and np.array_equal(result['original_mask'],before)
    assert (refined&truth).sum()/(refined|truth).sum()>.95
    assert not refined[99,82] and not refined[65,120]
    assert result['metrics']['boundary_support_fraction']>.7


def test_diffuse_shadow_flagged_without_deleting_original():
    rng,truth,image,valid,bg=fixture();y,x=np.mgrid[:180,:180]
    shadow=np.exp(-((x-90)**2+(y-90)**2)/(2*20**2))
    image=2000*(1-shadow[:,:,None]*np.array([.06,.08,.05]))+rng.normal(0,2,(180,180,3))
    seed=shadow>.55
    result=refine_candidate(image,seed,valid,[.2,.2])
    assert 'diffuse_or_weak_boundary' in result['flags']
    assert np.array_equal(result['original_mask'],seed) and result['status']=='review_required'


def test_valid_domain_is_never_expanded_and_edge_feature_flagged():
    rng,truth,image,valid,bg=fixture();valid[:,:48]=False
    result=refine_candidate(image,truth,valid,[.2,.2],substrate_mask=bg)
    assert not (result['refined_mask']&~valid).any()
    assert 'near_saved_domain_boundary' in result['flags']


def test_no_independent_background_retains_uncertain_original():
    _,truth,image,valid,bg=fixture()
    result=refine_candidate(image,truth,valid,[.2,.2],substrate_mask=np.zeros_like(bg))
    assert result['status']=='unresolved' and np.array_equal(result['refined_mask'],truth)


def test_dark_neighbor_alone_does_not_flag_clean_thick_flake_as_artifact():
    _,truth,image,valid,bg=fixture();image[110:145,140:160]=500
    result=refine_candidate(image,truth,valid,[.2,.2],substrate_mask=bg)
    assert 'diffuse_boundary_near_dark_feature_possible_shadow' not in result['flags']
    assert result['refined_mask'][80,80] and result['original_mask'][80,80]
    assert not result['likely_artifact']


def test_growth_is_bounded_and_remote_foreground_not_imported():
    from scipy.ndimage import distance_transform_edt
    _,truth,image,valid,bg=fixture();image[15:30,15:30]*=.92
    result=refine_candidate(image,truth,valid,[.2,.2],substrate_mask=bg)
    distance=distance_transform_edt(~truth,sampling=(.2,.2))
    assert not result['refined_mask'][20,20]
    assert np.max(distance[result['refined_mask']])<=2.


def test_diffuse_shadow_plus_strong_neighbor_is_reversible_likely_artifact():
    rng,truth,image,valid,bg=fixture();y,x=np.mgrid[:180,:180]
    shadow=np.exp(-((x-90)**2+(y-90)**2)/(2*20**2));seed=shadow>.55
    image=2000*(1-shadow[:,:,None]*np.array([.06,.08,.05]))+rng.normal(0,2,(180,180,3))
    image[45:135,118:145]=600
    result=refine_candidate(image,seed,valid,[.2,.2])
    assert result['likely_artifact'] and result['artifact_disposition']=='likely_artifact'
    assert np.array_equal(result['original_mask'],seed) and result['refined_mask'].any()


@pytest.mark.parametrize('tile,label,artifact',[('000329',153,False),('000639',121,False),('000660',167,True)])
def test_saved_lab_raw_replay_preserves_clean_flakes_and_flags_observed_shadow(tile,label,artifact):
    """Optional exact-data replay; never acquires data or rewrites an artifact."""
    import hashlib,json
    from pathlib import Path
    root=Path(__file__).resolve().parents[3]
    project=Path(json.loads((root/'configs/storage.json').read_text())['project_root'])
    run='scan-20261013-221727-131d9ced';base=project/'analysis/graphene'/run/'chip001'
    folder=base/'candidate-refinement-v1/candidates'/f'tile_{tile}-{label}'
    if not folder.exists():pytest.skip('Exact October14 saved refinement fixture unavailable')
    payload=json.loads((folder/'refinement.json').read_text())
    rawpath=project/'data'/run/'chip001/tiles'/f'tile_{tile}.tiff'
    assert hashlib.sha256(rawpath.read_bytes()).hexdigest()==payload['source_hashes'][f'tiles/tile_{tile}.tiff']
    ledger=json.loads((base/'full-chip-conditional-v1/ledger.json').read_text())
    fieldpath=base/'full-chip-conditional-v1'/ledger[f'tile_{tile}']['field_json']
    assert hashlib.sha256(fieldpath.read_bytes()).hexdigest()==payload['source_field_sha256']
    field=json.loads(fieldpath.read_text());factor=field['reduction']['factor'];x,y,w,h=payload['analysis_crop_xywh']
    raw=cv2.imread(str(rawpath),cv2.IMREAD_UNCHANGED)[:,:,::-1]
    cropped=raw[y*factor:(y+h)*factor,x*factor:(x+w)*factor]
    rgb=cropped.reshape(h,factor,w,factor,3).mean(axis=(1,3),dtype=np.float32)
    with np.load(fieldpath.with_name('masks.npz'),allow_pickle=False) as saved:
        section=np.s_[y:y+h,x:x+w]
        original=saved['labels'][section]==label;valid=saved['valid_mask'][section];support=saved['substrate_mask'][section]
    result=refine_candidate(rgb,original,valid,payload['pixel_size_um'],substrate_mask=support)
    assert result['likely_artifact'] is artifact
    assert np.array_equal(result['original_mask'],original)
    if not artifact:
        assert result['status']=='edge_supported_proposal'
        assert result['metrics']['boundary_support_fraction']>.9
        assert result['metrics']['original_retained_fraction']>.95
    else:assert result['metrics']['boundary_support_fraction']<.5
