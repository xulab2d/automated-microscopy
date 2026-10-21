import numpy as np
from flakefinder.model import Frame,Position
from flakefinder.measure_existing_regions import measure_existing_regions


def test_other_foreground_is_excluded_and_geometry_is_immutable():
    rng=np.random.default_rng(19);c=rng.normal(0,.003,(160,160,3)).astype('float32')
    labels=np.zeros((160,160),np.uint16);labels[55:95,55:95]=1
    labels[50:100,20:50]=2;labels[50:100,100:135]=3
    c[labels==1]+=[.04,.07,-.02];c[labels==2]+=[-.4,-.5,-.3];c[labels==3]+=[.3,.1,.2]
    labels[65:70,65:70]=0;c[65:70,65:70]=0 # known hole remains a hole
    before=labels.copy();f=Frame(np.ones_like(c),Position(0,0,0),(.25,.25),1)
    rows,report=measure_existing_regions(f,c,labels)
    assert set(rows)=={1,2,3} and np.array_equal(labels,before)
    assert rows[1]['measurement_status']=='measured'
    assert np.allclose(rows[1]['contrast_rgb'],[.04,.07,-.02],atol=.002)
    assert not report['mask_modified'] and rows[1]['mask_area_pixels']==int((labels==1).sum())


def test_missing_clear_substrate_preserves_unmeasurable_object():
    labels=np.ones((80,80),np.uint16);c=np.ones((80,80,3),np.float32)*.1
    f=Frame(np.ones_like(c),Position(0,0,0),(.25,.25),1)
    rows,_=measure_existing_regions(f,c,labels)
    assert set(rows)=={1}
    assert rows[1]['measurement_status']=='unmeasurable'
    assert rows[1]['reason']=='insufficient_clear_local_substrate'
    assert rows[1]['mask_area_pixels']==6400


def test_local_ratio_not_global_difference():
    rng=np.random.default_rng(7);c=np.full((120,120,3),.2)+rng.normal(0,.001,(120,120,3))
    labels=np.zeros((120,120),np.uint16);labels[40:80,40:80]=1;c[labels==1]+=.08
    f=Frame(np.ones_like(c),Position(0,0,0),(.25,.25),1)
    rows,_=measure_existing_regions(f,c,labels)
    assert np.allclose(rows[1]['contrast_rgb'],[.1,.1,.1],atol=.001)
