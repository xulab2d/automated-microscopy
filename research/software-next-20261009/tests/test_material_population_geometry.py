import numpy as np
import pytest

from flakefinder.material_contrast import MaterialContrastClassifier
from flakefinder.material_terraces import MaterialTerraceOptions,segment_material_terraces


def test_opt_in_merge_preserves_maps_holes_islands_unknowns_and_parent_boundaries():
    rng=np.random.default_rng(17)
    image=rng.normal(0,.001,(72,72,3)).astype(np.float32)
    parents=np.zeros((72,72),np.int32)
    parents[18:42,18:42]=1
    parents[22:38,42:48]=1  # Connected unknown material remains separate.
    parents[24:32,54:62]=1  # Same source parent, disconnected island.
    parents[10:18,18:42]=2  # Touching but independently supplied parent.
    a=np.array([.12,.04,-.04]);b=np.array([.24,.08,-.08])
    for x in range(18,42):image[18:42,x]=a if (x//3)%2 else b
    image[24:32,54:62]=a;image[10:18,18:42]=a
    image[22:38,42:48]=[-.3,.3,.2]
    image[28:32,28:32]=0.  # Real substrate hole cannot be filled.
    classifier=MaterialContrastClassifier({'kind':'empirical_material_populations','components':[
        {'label':'a','role':'material','mean_rgb':a.tolist(),'covariance_rgb':(np.eye(3)*.003**2).tolist()},
        {'label':'b','role':'material','mean_rgb':b.tolist(),'covariance_rgb':(np.eye(3)*.003**2).tolist()}]})
    args=(image,np.ones((72,72),bool),parents==0,np.eye(3)*.001**2,(1.,1.),classifier)
    options=dict(smoothing_um=0.,interior_erosion_um=0.)
    old=segment_material_terraces(*args,parent_labels=parents,options=MaterialTerraceOptions(**options))
    explicit=segment_material_terraces(*args,parent_labels=parents,options=MaterialTerraceOptions(**options,merge_material_populations=False))
    merged=segment_material_terraces(*args,parent_labels=parents,options=MaterialTerraceOptions(**options,merge_material_populations=True))
    assert np.array_equal(old['labels'],explicit['labels'])
    assert np.array_equal(old['class_map'],merged['class_map'])
    assert np.array_equal(parents,merged['parent_labels'])
    assert np.array_equal(old['material_compatible_mask'],merged['material_compatible_mask'])
    grouped=[r for r in merged['regions'] if r.get('material_population_merged')]
    assert len(grouped)==3  # Main flake, island and independent second parent.
    main=max(grouped,key=lambda r:r['area_um2'])
    assert main['area_um2']==24*24-4*4 and main['holes_px']
    assert main['constituent_class_indices']==[1,2]
    assert main['class_index']==-3
    assert main['class_name']=='material-compatible optical feature'
    assert main['material_pixel_fraction']==1.
    assert not main['usable_rectangle']['available']
    # Compatibility of two distinct populations does not certify their mean.
    assert not main['selected_for_review']
    assert np.all(merged['labels'][28:32,28:32]==0)
    assert merged['labels'][20,20]!=merged['labels'][26,56]
    assert merged['labels'][20,20]!=merged['labels'][14,20]
    unknown=[r for r in merged['regions'] if r['class_index']==-1]
    assert len(unknown)==1 and unknown[0]['area_um2']==16*6


def test_merge_option_rejects_non_boolean():
    with pytest.raises(ValueError,match='boolean'):
        MaterialTerraceOptions(merge_material_populations='yes')
