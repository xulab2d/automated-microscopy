import numpy as np
from flakefinder.model import Frame,Position
from flakefinder.thin_regions import ThinRegionOptions,propose_thin_regions,_local_evidence


def test_faint_mixed_sign_plateau_is_detected_without_material_reference():
    rng=np.random.default_rng(17)
    contrast=rng.normal(0,.01,(192,192,3)).astype('float32')
    truth=np.zeros(contrast.shape[:2],bool);truth[60:112,64:116]=True
    contrast[truth]+=[.01,.02,-.005]
    frame=Frame(np.ones_like(contrast),Position(0,0,0),(.25,.25),1)
    labels,rows,report=propose_thin_regions(frame,contrast,options=ThinRegionOptions(block_size_um=.5))
    ids,counts=np.unique(labels[truth],return_counts=True)
    best=max(((int(i),int(n)) for i,n in zip(ids,counts) if i),key=lambda t:t[1])[0]
    mask=labels==best
    assert (mask&truth).sum()/(mask|truth).sum()>.9
    assert rows[best]['contrast_rgb'][2]<0
    assert rows[best]['material_identity'] is None
    assert report['separated_spatial_measurement_partition']


def test_identical_disconnected_plateaus_do_not_pool_area():
    rng=np.random.default_rng(9)
    c=rng.normal(0,.003,(120,200,3)).astype('float32')
    c[40:80,20:60]+=[.04,.08,0]
    c[40:80,130:170]+=[.04,.08,0]
    f=Frame(np.ones_like(c),Position(0,0,0),(.25,.25),1)
    labels,rows,_=propose_thin_regions(f,c,options=ThinRegionOptions(block_size_um=.5))
    left=labels[50:70,30:50];right=labels[50:70,140:160]
    a=np.bincount(left.ravel()).argmax();b=np.bincount(right.ravel()).argmax()
    assert a>0 and b>0 and a!=b
    assert rows[a]['area_um2']<120 and rows[b]['area_um2']<120


def test_local_weber_ratio_uses_local_substrate_denominator():
    block=np.full((60,60,3),.2,dtype='float32');block[20:40,20:40]=.28
    labels=np.zeros((60,60),np.int32);labels[20:40,20:40]=1
    row=_local_evidence(block,np.ones((60,60),bool),labels,1,(slice(20,40),slice(20,40)),
        np.eye(3)*.001,ThinRegionOptions(),(1.,1.))
    assert np.allclose(row['contrast_rgb'],[.1,.1,.1],atol=1e-5)
    assert np.linalg.eigvalsh(row['contrast_covariance']).min()>0
