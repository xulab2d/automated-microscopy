import cv2
import numpy as np

from flakefinder.colors import AnalysisOptions,prepare_contrast
from flakefinder.global_domains import global_domain_labels
from flakefinder.model import Frame,Position


def classify(image):
    frame=Frame(np.uint16(image),Position(0,0,0),(.5,.5),0)
    options=AnalysisOptions(max_components=6)
    prepared=prepare_contrast(frame,options)
    # The new backend must not accidentally depend on old object proposals.
    prepared['foreground'][:]=False
    return global_domain_labels(frame,prepared,options)


def test_global_population_preserves_faint_large_region_and_hole_without_object_gate():
    rng=np.random.default_rng(81)
    image=np.full((240,300,3),2000.,float)
    target=np.zeros((240,300),bool);target[70:145,90:180]=True;target[95:110,120:140]=False
    image[target]*=[.988,.98,.99];image+=rng.normal(0,1,image.shape)
    labels,evidence,report=classify(image)
    best=max(evidence,key=lambda k:np.count_nonzero((labels==k)&target))
    got=labels==best
    assert np.count_nonzero(got&target)/np.count_nonzero(got|target)>.85
    assert not got[102,130]
    assert report['region_count']==len(evidence)
    assert evidence[best]['segmentation_method']=='whole_field_color_population_connected_domain'


def test_disconnected_same_color_particles_never_share_instance_or_intervening_substrate():
    image=np.full((240,320,3),2000.,float)
    for x,y in [(60,70),(110,85),(75,130)]:image[y:y+25,x:x+25]*=[.65,.6,.7]
    labels,evidence,_=classify(image)
    ids=[int(labels[y+12,x+12]) for x,y in [(60,70),(110,85),(75,130)]]
    assert min(ids)>0 and len(set(ids))==3
    assert labels[110,103]==0
    assert all(np.count_nonzero(labels==k)<900 for k in ids)
