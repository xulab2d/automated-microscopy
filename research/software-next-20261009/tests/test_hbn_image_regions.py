import inspect
import cv2
import numpy as np

from flakefinder.hbn_image_regions import image_regions


def scene():
    rng=np.random.default_rng(141)
    shape=(140,180);parents=np.zeros(shape,np.int32)
    cv2.fillPoly(parents,[np.array([[35,100],[145,100],[55,25]],np.int32)],1)
    # An observed hole is never filled, even though the surrounding region is
    # a strong, contiguous optical population.
    parents[72:80,66:74]=0
    thin=parents==1
    rim=np.zeros(shape,np.uint8);cv2.line(rim,(36,98),(55,27),1,7)
    rim=(rim>0)&thin;thin=thin&~rim
    c=rng.normal(0,.003,shape+(3,)).astype(np.float32)
    c[thin]+=[-.06,-.035,-.045]
    c[rim]+=[-.55,-.40,-.20]
    substrate=parents==0
    return c,parents,thin,rim,substrate


def majority(labels,mask):
    return np.bincount(labels[mask]).argmax()


def test_faint_polygon_kept_bright_edge_separate_hole_preserved():
    c,parents,thin,rim,substrate=scene()
    result=image_regions(c,np.ones(parents.shape,bool),substrate,np.eye(3)*.003**2,[.4,.4],parents)
    labels=result['labels'];thin_id=majority(labels,thin);rim_id=majority(labels,rim)
    assert thin_id>0 and rim_id>0 and thin_id!=rim_id
    interior=cv2.erode(thin.astype(np.uint8),np.ones((5,5),np.uint8))>0
    assert (labels[interior]==thin_id).mean()>.97
    assert np.array_equal(labels>0,parents>0)
    assert not labels[72:80,66:74].any()
    assert np.array_equal(result['parent_labels'],parents)


def test_no_chemical_classes_enter_geometry_and_inferred_parent_covers_interior():
    assert not any('class' in p or 'thickness' in p for p in inspect.signature(image_regions).parameters)
    c,parents,thin,rim,substrate=scene()
    result=image_regions(c,np.ones(parents.shape,bool),substrate,np.eye(3)*.003**2,[.4,.4])
    interior=cv2.erode(thin.astype(np.uint8),np.ones((7,7),np.uint8))>0
    assert (result['labels'][interior]>0).mean()>.99
    assert result['report']['chemical_class_used_for_geometry'] is False
    assert not result['labels'][74:78,68:72].any()


def test_disconnected_islands_and_narrow_observed_features_are_retained():
    c,parents,thin,rim,substrate=scene()
    parents[10:12,15:90]=2;parents[125:127,15:90]=2
    c[parents==2]=[-.07,-.04,-.05]
    result=image_regions(c,np.ones(parents.shape,bool),parents==0,np.eye(3)*.003**2,[.4,.4],parents)
    labels=result['labels']
    assert labels[10,40]>0 and labels[125,40]>0
    assert labels[10,40]!=labels[125,40]
    assert np.array_equal(labels>0,parents>0)
