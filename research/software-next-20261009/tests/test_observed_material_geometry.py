"""Observed image geometry cannot be carved by a conditional optical classifier."""
import numpy as np
import pytest
from flakefinder.material_contrast import MaterialContrastClassifier
from flakefinder.material_terraces import MaterialTerraceOptions,segment_material_terraces
from test_analyze_graphene_run import driver


def fixture():
    rng=np.random.default_rng(31)
    contrast=rng.normal(0,.001,(64,72,3)).astype(np.float32)
    parents=np.zeros((64,72),np.int32);parents[15:45,15:45]=1
    parents[25:29,25:29]=0 # Observed hole remains a hole.
    parents[19:26,55:62]=1 # A separate island cannot be joined.
    parents[45:49,15:45]=2 # Touching independent parent cannot be merged.
    contrast[parents>0]=[.12,.04,-.04]
    contrast[15:45,29:33]=[-.25,.2,.3] # Unknown optical stripe stays in observed object.
    contrast[15:45,35:38]=0 # Null-class stripe cannot split observed mask either.
    model={'kind':'empirical_material_populations','components':[
        {'label':'a','role':'material','mean_rgb':[.12,.04,-.04],'covariance_rgb':(np.eye(3)*.003**2).tolist()}]}
    reduced={'contrast_rgb':contrast,'valid_mask':np.ones(parents.shape,bool),'substrate_mask':parents==0,
        'noise_covariance':np.eye(3)*.001**2,'pixel_size_um':np.array([1.,1.]),'saturated_mask':np.zeros(parents.shape,bool)}
    return reduced,parents,model


def test_preserve_supplied_geometry_including_null_unknown_holes_and_islands():
    reduced,parents,model=fixture();classifier=MaterialContrastClassifier(model)
    args=tuple(reduced[k] for k in ('contrast_rgb','valid_mask','substrate_mask','noise_covariance','pixel_size_um'))+(classifier,)
    options=dict(smoothing_um=0.,interior_erosion_um=0.)
    old=segment_material_terraces(*args,parent_labels=parents,options=MaterialTerraceOptions(**options))
    observed=segment_material_terraces(*args,parent_labels=parents,options=MaterialTerraceOptions(**options,preserve_parent_regions=True))
    assert np.array_equal(observed['labels']>0,parents>0)
    assert np.array_equal(observed['parent_labels'],parents)
    for key in ('class_map','material_compatible_mask','debris_compatible_mask'):
        assert np.array_equal(observed[key],old[key])
    assert len(observed['regions'])==3 and len(old['regions'])>3
    main=max(observed['regions'],key=lambda r:r['area_um2'])
    assert main['holes_px'] and set(main['constituent_class_indices'])=={-1,0,1}
    assert main['geometry_qualification'].startswith('Supplied observed image regions')
    assert not main['usable_rectangle']['available'] # No uniform terrace fabricated.
    assert main['contrast_rgb'] and main['sieve_results'] and main['class_index']==-4
    assert observed['labels'][20,20]!=observed['labels'][20,56]
    assert observed['labels'][44,20]!=observed['labels'][46,20]


def test_preservation_requires_explicit_valid_labels():
    with pytest.raises(ValueError,match='boolean'):MaterialTerraceOptions(preserve_parent_regions='yes')
    with pytest.raises(ValueError,match='not both'):MaterialTerraceOptions(preserve_parent_regions=True,merge_material_populations=True)
    reduced,parents,model=fixture()
    args=tuple(reduced[k] for k in ('contrast_rgb','valid_mask','substrate_mask','noise_covariance','pixel_size_um'))+(MaterialContrastClassifier(model),)
    with pytest.raises(ValueError,match='requires supplied'):segment_material_terraces(*args,options=MaterialTerraceOptions(preserve_parent_regions=True))
    saturated=np.zeros(parents.shape,bool);saturated[20,20]=True
    with pytest.raises(ValueError,match='unsaturated'):segment_material_terraces(*args,parent_labels=parents,saturated_mask=saturated,options=MaterialTerraceOptions(preserve_parent_regions=True))


def test_driver_observed_opt_in_and_default_graphene_use_independent_geometry(monkeypatch):
    reduced,parents,model=fixture();classifier=MaterialContrastClassifier(model)
    from flakefinder import hbn_image_regions
    calls=[]
    def regions(*args,**kwargs):
        calls.append(True)
        return {'labels':parents,'parent_labels':parents,'diagnostics':[],
            'regions':[{'instance_label':i,'parent_instance_label':i,'photometric_mode':0,'geometry_basis':'test_image'} for i in (1,2)],
            'report':{'chemical_class_used_for_geometry':False}}
    monkeypatch.setattr(hbn_image_regions,'image_regions',regions)
    old=driver.segment_reduced_input(reduced,model,classifier,'graphene',lambda:None)
    assert calls==[] and old['report']['method']=='pixel_population_classification_then_connected_regions'
    result=driver.segment_reduced_input(reduced,{**model,'geometry_method':'observed_rgb_v1'},classifier,'hBN',lambda:None)
    assert calls==[True] and np.array_equal(result['labels']>0,parents>0)
    assert np.array_equal(result['class_map'],old['class_map'])
    assert result['report']['image_geometry']['chemical_class_used_for_geometry'] is False
    assert all(r['geometry_basis']=='test_image' for r in result['regions'])
    with pytest.raises(ValueError,match='Unsupported'):driver.segment_reduced_input(reduced,{'geometry_method':'typo'},classifier,'hBN',lambda:None)


def test_analyze_field_serializes_observed_masks_and_raw_bound_evidence(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from test_hbn_region_evidence import model as optical_model
    rng=np.random.default_rng(22)
    contrast=rng.normal(0,.001,(80,80,3)).astype(np.float32)
    contrast[16:56,16:56]+=[.1,-.1,-.2]
    support=np.ones((80,80),bool);support[12:60,12:60]=False
    arrays={'contrast_rgb':contrast,'valid_mask':np.ones(support.shape,bool),'substrate_mask':support,
        'saturated_mask':np.zeros(support.shape,bool),'pixel_size_um':np.array([.5,.5])}
    raw=tmp_path/'chip001/tiles/tile_000001.tiff';raw.parent.mkdir(parents=True);raw.write_bytes(b'synthetic raw pin')
    digest=driver.digest(raw)
    bundle={**optical_model(),'calibration_status':'synthetic_unvalidated','caveats':[],
        'geometry_method':'observed_rgb_v1','alternative_models':[optical_model()],
        'operator_annotations':[{'tile':'tile_000001','point_px':[30,30],'label':'thin_positive',
            'source':'synthetic test annotation','source_raw_sha256':digest}]}
    entry={'chip':raw.parent.parent,'tile':'tile_000001','field':'chip001/tile_000001',
        'context':{'material':'hBN'},'source_hashes':{'tiles/tile_000001.tiff':digest},'record_sha256':'test-record',
        'record':{'position':{'x':0,'y':0,'z':0}},'transform':{'matrix':[[.5,0],[0,.5]]}}
    monkeypatch.setattr(driver.watcher,'input_context',lambda *a,**k:{'material':'hBN'})
    monkeypatch.setattr('flakefinder.material_input.prepare_material_input',lambda **k:(arrays,{'synthetic':True}))
    args=SimpleNamespace(run_id='synthetic',exploratory_context_mismatch=True,factor=2)
    output=tmp_path/'attempt'
    value=driver.analyze_field(entry,bundle,output,args,lambda:None)
    assert value['assessment_state']=='assessed' and value['regions']
    assert value['hbn_evidence_binding']['bound_annotations']==1
    positive=[r for r in value['regions'] if r['hbn_screen']['operator_supported']]
    assert len(positive)==1 and positive[0]['hbn_screen']['annotation_bindings'][0]['source_raw_sha256']==digest
    assert positive[0]['hbn_screen']['measurement_source']['source_pixel_from_analysis_pixel']==[[2.,0.,.5],[0.,2.,.5]]
    assert value['report']['image_geometry']['chemical_class_used_for_geometry'] is False
    with np.load(output/'masks.npz') as saved:
        assert 'image_parent_labels' in saved.files and 'class_map' in saved.files
        assert np.array_equal(saved['labels']>0,saved['parent_labels']>0)
    assert raw.read_bytes()==b'synthetic raw pin'
