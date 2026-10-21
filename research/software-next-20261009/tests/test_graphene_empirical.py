import numpy as np
import pytest
from flakefinder.graphene_empirical import build_model,EmpiricalGrapheneClassifier


def fixture():
    def row(i,r,g,b):return {'id':str(i),'contrast_rgb':[r,g,b],'below_reported_line':True}
    obs={'run_id':'only-this-run','first_region':{'consistency':[row(1,.10,.086,.079),row(2,.14,.105,.067)]},
         'second_region':{'inferred_outlier_ids':['bad1','bad2'],'saved_points':[row(3,.25,.193,.12),row(4,.27,.201,.129),row('bad1',.217,.228,.230),row('bad2',.236,.231,.193)]},
         'third_region':{'saved_points':[row(5,.35,.271,.175),row(6,.362,.287,.182)]}}
    model=build_model(obs,{'optical_profile_id':'only-this-run'},'a'*64)
    return model,EmpiricalGrapheneClassifier(model,context={'optical_profile_id':'only-this-run'},model_covariance=np.eye(3)*.0001)


def test_supported_bands_negative_outliers_and_continuous_interpolation():
    model,c=fixture();points=np.array([[.1,.086,.079],[.25,.193,.12],[.362,.287,.182],[.2,.154,.096],[.217,.228,.230],[.236,.231,.193]])
    r=c.classify(points,np.eye(3)*4e-6)
    assert r['material_compatible'].tolist()==[True]*4+[False]*2
    assert r['debris_reference_compatible'].tolist()==[False]*4+[True]*2
    assert r['reference_distance'][-1]>r['reference_distance'][1]
    assert not c.metadata['layer_assignment_allowed']


def test_extrapolation_noise_and_unknowns_not_promoted_or_deleted():
    _,c=fixture();r=c.classify(np.array([[0,0,0],[.44,.3388,.2112],[.8,.1,.3],[.001,.001,.001]]),np.eye(3)*4e-6)
    assert r['class_index'].tolist()==[0,3,-1,0]
    assert not r['material_compatible'].any()
    assert r['unknown'].tolist()==[False,True,True,False]


def test_run_context_is_required():
    model,_=fixture()
    with pytest.raises(ValueError,match='context mismatch'):EmpiricalGrapheneClassifier(model,context={'optical_profile_id':'other-run'})


def test_pixel_segmentation_changes_masks_and_preserves_unclassified_objects():
    from flakefinder.material_terraces import segment_material_terraces,MaterialTerraceOptions
    _,classifier=fixture();image=np.zeros((100,160,3),float)
    image[15:45,15:45]=[.25,.193,.12]
    image[15:45,65:95]=[.217,.228,.23]
    image[55:85,110:140]=[.8,.1,.3]
    valid=np.ones(image.shape[:2],bool);substrate=np.all(image==0,axis=2)
    result=segment_material_terraces(image,valid,substrate,np.eye(3)*4e-6,[1.,1.],classifier,options=MaterialTerraceOptions(useful_area_um2=25.))
    assert result['material_compatible_mask'][30,30]
    assert result['debris_compatible_mask'][30,80]
    assert not result['material_compatible_mask'][70,125]
    assert result['labels'][70,125]>0
    assert any(r['selected_for_review'] for r in result['regions'])
    assert any(not r['selected_for_review'] for r in result['regions'])


def test_noisy_background_cannot_promote_first_band_achromatic_features():
    _,c=fixture();points=np.array([[.127,.121,.119],[.125,.115,.113],[.12,.094,.062]])
    for noise in (.000004,.0004):
        r=c.classify(points,np.eye(3)*noise)
        assert not r['material_compatible'][:2].any()
    assert c.classify(points,np.eye(3)*.000004)['material_compatible'][2]


def test_bounded_source_geometry_equals_full_image_scan():
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
    from analyze_graphene_run import source_geometry
    from scipy.ndimage import find_objects
    labels=np.zeros((120,150),np.int32);labels[29:53,47:69]=1;labels[35:40,50:54]=0
    region={'instance_label':1,'polygon_px':[[47,29],[68,29],[68,52]],'holes_px':[]}
    entry={'record':{'position':{'x':100.,'y':200.,'z':30.}},'transform':{'matrix':[[.2,0],[0,-.2]]}}
    reduction={'factor':2,'source_shape_hw':[240,300]}
    assert source_geometry(region,labels,entry,reduction)==source_geometry(region,labels,entry,reduction,find_objects(labels)[0])
