from pathlib import Path
import numpy as np
import pytest

from flakefinder.material_profiles import load_material_profile,predict_material_contrast,predict_material_ensemble
from flakefinder.material_contrast import MaterialContrastClassifier,fit_empirical_material_model,import_2dmatgmm_graphene
from flakefinder.material_terraces import MaterialTerraceOptions,segment_material_terraces

CONFIG=Path(__file__).resolve().parents[1]/'configs/materials'


def test_material_specific_spectral_zero_and_hbn_reversal():
    graphene=load_material_profile(CONFIG/'graphene-sio2-90nm.json')
    hbn=load_material_profile(CONFIG/'hbn-sio2-300nm.json')
    g=graphene.forward([0,.34,.68],wavelengths_nm=[450,550,650])
    h=hbn.forward([0,.333,.666],wavelengths_nm=[450,550,650],oxide_nm=290)
    assert 'contrast_rgb' not in g
    assert np.allclose(g['spectral_contrast'][0],0,atol=1e-12)
    assert np.all(np.asarray(g['reflectance'])<=1)
    # Published distinction: transparent BN is brighter at short wavelengths and
    # darker at long wavelengths on~290nm; graphene must not inherit a BN box.
    assert h['spectral_contrast'][1][0]<0<h['spectral_contrast'][1][-1]
    assert not h['layer_assignment_allowed']


def test_rgb_needs_response_and_channel_gain_cancels():
    profile=load_material_profile(CONFIG/'graphene-sio2-90nm.json')
    response={'kind':'synthetic_delta_channels','source':'synthetic test only','radiometry_status':'synthetic linear',
        'wavelengths_nm':[450,550,650],'effective_weight_rgb':np.eye(3)[::-1].tolist()}
    a=profile.forward([0,.34],wavelengths_nm=[450,550,650],response=response)
    response['effective_weight_rgb']=(np.eye(3)[::-1]*[2,.2,3]).tolist()
    b=profile.forward([0,.34],wavelengths_nm=[450,550,650],response=response)
    assert np.allclose(a['contrast_rgb'],b['contrast_rgb'])
    with pytest.raises(ValueError,match='response'):MaterialContrastClassifier(profile.forward([0,.34]))
    with pytest.raises(ValueError,match='Wavelength'):profile.forward([0,.34],wavelengths_nm=[350,600])


def empirical_fixture():
    return {'kind':'empirical_material_populations','model_id':'synthetic-known-pixels','contrast_convention':'(substrate-flake)/substrate',
        'calibration_status':'synthetic_only','components':[
            {'label':'plateau-A','role':'material','mean_rgb':[.1,.15,.06],'covariance_rgb':(np.eye(3)*.000025).tolist()},
            {'label':'debris-control','role':'debris','mean_rgb':[.12,-.05,.06],'covariance_rgb':(np.eye(3)*.000025).tolist()}]}


def test_substrate_null_unknown_debris_and_context_not_false_layer():
    model=empirical_fixture();model['applicability_context']={'oxide_nm':90,'profile_id':'fixed'}
    with pytest.raises(ValueError,match='context'):MaterialContrastClassifier(model,context={'oxide_nm':285})
    c=MaterialContrastClassifier(model,context={'oxide_nm':90,'profile_id':'fixed'})
    result=c([[0,0,0],[.1,.15,.06],[.12,-.05,.06],[-.4,.2,.5]],np.eye(3)*.0001)
    assert result['class_index'].tolist()==[0,1,2,-1]
    assert result['material_compatible'].tolist()==[False,True,False,False]
    assert result['debris_reference_compatible'][2]
    assert not result['metadata']['layer_assignment_allowed']
    with pytest.raises(ValueError,match='positive definite'):c([0,0,0],np.zeros((3,3)))


def test_empirical_training_balances_independent_groups_and_holds_groups_out():
    rng=np.random.default_rng(11);samples=[]
    for group,size in [('a',100),('b',1000),('held',100)]:
        samples.append({'label':'operator-material','role':'material','group_id':group,'source':'synthetic_'+group,
            'contrast_rgb':rng.normal([.1,.15,.06],.003,(size,3)).tolist()})
    m=fit_empirical_material_model(samples,context={'oxide_nm':90},model_id='test',validation_groups=['held'])
    assert m['components'][0]['independent_training_groups']==2
    assert m['components'][0]['balanced_pixels_per_group']==100
    assert [v['group_id'] for v in m['validation']]==['held']
    assert not m['layer_assignment_allowed']
    with pytest.raises(ValueError,match='independent training'):fit_empirical_material_model(samples[:1],context={'oxide_nm':90},model_id='test')


def test_pixelwise_terraces_preserve_holes_disconnected_islands_and_all_parents():
    rng=np.random.default_rng(4);shape=(160,200);image=rng.normal(0,.002,shape+(3,)).astype(np.float32)
    parents=np.zeros(shape,np.int32);parents[20:70,20:75]=7;parents[90:140,100:170]=7
    parents[35:50,35:50]=0
    image[parents==7]+=[.1,.15,.06]
    support=parents==0;valid=np.ones(shape,bool)
    result=segment_material_terraces(image,valid,support,np.eye(3)*.002**2,(1,1),MaterialContrastClassifier(empirical_fixture()),parent_labels=parents,
        options=MaterialTerraceOptions(smoothing_um=0,source_floor_um2=2))
    assert np.array_equal(result['parent_labels'],parents)
    assert np.all(result['labels'][35:50,35:50]==0)
    assert result['labels'][25,25]!=result['labels'][100,110]
    assert len([r for r in result['regions'] if r['selected_for_review']])==2
    assert all(r['layer_number'] is None for r in result['regions'])
    assert any(r['holes_px'] for r in result['regions'])
    for row in result['regions']:
        rectangle=row['usable_rectangle']
        if rectangle['available']:
            x,y,w,h=rectangle['bbox_px']
            assert np.all(result['labels'][y:y+h,x:x+w]==row['instance_label'])
            assert rectangle['area_um2']<=row['area_um2']


def test_bare_substrate_control_does_not_become_a_material_parent():
    rng=np.random.default_rng(18);image=rng.normal(0,.003,(160,200,3)).astype(np.float32)
    result=segment_material_terraces(image,np.ones((160,200),bool),np.ones((160,200),bool),np.eye(3)*.003**2,(.2,.2),MaterialContrastClassifier(empirical_fixture()))
    assert not any(r['selected_for_review'] for r in result['regions'])
    assert result['report']['selected_regions']==0


def test_external_prior_is_explicit_foreign_context_and_rgb_sign():
    import json
    model=json.loads((CONFIG/'graphene90-2dmatgmm-external.json').read_text())
    assert np.allclose(model['components'][0]['mean_rgb'],[.137,.14,.071])
    with pytest.raises(ValueError,match='context'):MaterialContrastClassifier(model,context={'material':'graphene','oxide_nm':90})
    assert not model['layer_assignment_allowed']


def test_small_color_supported_feature_is_ranked_not_deleted():
    rng=np.random.default_rng(15);image=rng.normal(0,.002,(100,100,3)).astype(np.float32)
    parent=np.zeros((100,100),np.int32);parent[35:55,35:55]=1;image[parent==1]+=[.1,.15,.06]
    result=segment_material_terraces(image,np.ones(parent.shape,bool),parent==0,np.eye(3)*.002**2,(.2,.2),MaterialContrastClassifier(empirical_fixture()),parent_labels=parent,
        options=MaterialTerraceOptions(smoothing_um=0))
    row=max(result['regions'],key=lambda r:r['area_um2'])
    assert row['area_um2']==pytest.approx(16.)
    assert row['selected_for_review'] and row['small_feature_requires_extra_review']
    assert row['priority']=='small_feature_edge_shape_review' and row['size_is_ranking_only']


def test_model_export_obeys_managed_destination_and_refuses_overwrite(tmp_path,monkeypatch):
    from flakefinder.material_contrast import save_material_model
    from flakefinder import storage
    destination=tmp_path/'managed'/'model.json';seen=[]
    def resolve(path):seen.append(path);return destination
    monkeypatch.setattr(storage,'resolve_output_path',resolve)
    receipt=save_material_model(empirical_fixture(),'logical/models/model.json')
    assert seen==['logical/models/model.json'] and Path(receipt['path'])==destination
    with pytest.raises(FileExistsError):save_material_model(empirical_fixture(),'logical/models/model.json')
    with pytest.raises(ValueError,match='illustrative'):save_material_model(empirical_fixture(),tmp_path/'unmanaged.json',managed_output=False)


def test_raw_preparation_preserves_domain_not_background_membership(tmp_path):
    import json
    import cv2
    from flakefinder.material_input import prepare_material_input
    rng=np.random.default_rng(72);rgb=np.uint16(np.clip(rng.normal(2000,4,(128,160,3)),0,4095))
    rgb[40:65,45:80]=np.uint16(rgb[40:65,45:80]*[.9,.85,.94])
    path=tmp_path/'raw.tiff';assert cv2.imwrite(str(path),rgb[:,:,::-1])
    path.with_suffix('.json').write_text(json.dumps({'position':{'x':0,'y':0,'z':0},'pixel_size_um':[.2,.2],
        'sequence':0,'acquired_at':0,'metadata':{'white_level':4095}}))
    valid=np.ones((128,160),np.uint8);support=valid.copy();support[35:70,40:85]=0
    cv2.imwrite(str(tmp_path/'domain.png'),valid);cv2.imwrite(str(tmp_path/'substrate.png'),support)
    context={'material':'graphene','oxide_nm':90,'optical_profile_id':'synthetic-raw',
        'radiometry':{'raw_linear_status':'verified','response_assumption':'synthetic only','dark_offset_status':'measured','dark_offset_rgb':[0,0,0]}}
    arrays,provenance=prepare_material_input(tiff=path,domain_mask=tmp_path/'domain.png',substrate_mask=tmp_path/'substrate.png',context=context)
    assert arrays['valid_mask'][50,50] and not arrays['substrate_mask'][50,50]
    assert np.allclose(arrays['contrast_rgb'][50,50],[.1,.15,.06],atol=.01)
    assert provenance['raw_sha256'] and provenance['domain_evidence']['domain_sha256']
    assert np.linalg.eigvalsh(arrays['noise_covariance']).min()>0
    with pytest.raises(ValueError,match='raw_linear_status'):
        prepare_material_input(tiff=path,domain_mask=tmp_path/'domain.png',substrate_mask=tmp_path/'substrate.png',context={**context,'radiometry':{}})
    with pytest.raises(ValueError,match='requires tiff'):
        prepare_material_input(tiff=path,context=context)
