import cv2
import numpy as np
from flakefinder.hbn_halo_screen import screen_halos


def scene():
    labels=np.zeros((140,170),np.int32)
    labels[30:96,35:101]=1;labels[33:93,38:98]=2
    rgb=np.ones(labels.shape+(3,),np.float32)
    rgb[labels==1]+=[.1,.12,.08];rgb[labels==2]+=[.7,.84,.56]
    noise=np.ones(3)*.01
    prepared={'normalized':rgb,'smooth':(rgb-1)/noise,'noise':noise}
    return labels,prepared,[{'instance_label':1},{'instance_label':2}]


def test_observed_halo_conjunction_and_reversible_operator_override():
    labels,p,regions=scene();before=labels.copy()
    result=screen_halos(regions,labels,p,[.3,.3])
    assert result[1]['likely_halo']
    assert not result[2]['likely_halo']
    assert result[1]['metrics']['near_stronger_neighbor_fraction']>.9
    regions[0]['hbn_screen']={'operator_supported':True}
    result=screen_halos(regions,labels,p,[.3,.3])
    assert result[1]['observed_halo_conjunction']
    assert not result[1]['likely_halo']
    assert result[1]['operator_positive_override']
    assert np.array_equal(labels,before)


def test_isolated_thin_ribbon_and_broad_triangle_are_not_halos():
    labels,p,regions=scene()
    labels[115:118,15:155]=3
    cv2.fillPoly(labels,[np.array([[107,25],[164,80],[107,80]],np.int32)],4)
    for id in (3,4):p['normalized'][labels==id]=[1.1,1.12,1.08]
    p['smooth']=(p['normalized']-1)/p['noise']
    result=screen_halos(regions+[{'instance_label':3},{'instance_label':4}],labels,p,[.3,.3])
    assert not result[3]['likely_halo']
    assert not result[4]['likely_halo']
    assert result[4]['status']=='substantial_observed_interior'


def test_missing_exterior_or_no_stronger_neighbor_cannot_reject_narrow_region():
    labels,p,regions=scene()
    result=screen_halos(regions,labels,p,[.3,.3],valid_mask=labels>0)
    assert not result[1]['likely_halo']
    p['smooth'][labels==2]=p['smooth'][labels==1][0]*1.2
    result=screen_halos(regions,labels,p,[.3,.3])
    assert not result[1]['likely_halo']


def test_check_cancel_is_respected():
    labels,p,regions=scene()
    def stop():raise RuntimeError('stop')
    import pytest
    with pytest.raises(RuntimeError,match='stop'):screen_halos(regions,labels,p,[.3,.3],check_cancel=stop)


def test_one_rim_can_wrap_two_separate_stronger_lobes_without_merging_them():
    labels,p,regions=scene()
    labels[33:93,68:98]=3
    labels[33:93,66:68]=0
    p['normalized'][labels==0]=1;p['smooth']=(p['normalized']-1)/p['noise']
    result=screen_halos(regions+[{'instance_label':3}],labels,p,[.3,.3])
    assert result[1]['likely_halo']
    assert set(result[1]['metrics']['neighbor_instance_labels'])=={2,3}
    assert (labels[33:93,66:68]==0).all()
