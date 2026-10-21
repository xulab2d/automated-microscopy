"""Object-level controls: useful size, terraces, diffuse haze and non-crystal shape."""
import cv2
import numpy as np
from flakefinder.colors import AnalysisOptions,ColorGroups,prepare_contrast
from flakefinder.analysis import extract_regions
from flakefinder.model import Frame,Position


def analyze(image, pixel=.5):
    frame=Frame(np.uint16(image),Position(0,0,0),(pixel,pixel),0)
    options=AnalysisOptions(max_components=4)
    prepared=prepare_contrast(frame,options)
    groups=ColorGroups.fit(prepared['smooth'][prepared['foreground']],4)
    return extract_regions(frame,prepared,groups,options)[0]


def test_touching_terraces_remain_one_useful_parent_object():
    rng=np.random.default_rng(34)
    image=np.full((360,440,3),2000.,float)
    image[100:180,100:140]*=[.93,.89,.97]
    image[100:180,140:190]*=[.86,.78,.94]
    image+=rng.normal(0,2,image.shape)
    rows=analyze(image)
    candidates=[r for r in rows if r['disposition']=='candidate']
    assert len(candidates)==1
    assert candidates[0]['area_um2']>1700
    assert len(candidates[0]['terraces'])==2
    assert all(t['area_um2']>700 for t in candidates[0]['terraces'])


def test_area_and_interior_width_are_physical_not_pixel_thresholds():
    for pixel in (.25,.5):
        image=np.full((640,640,3),2000.,float)
        # A 3 x 3 µm crystal is deliberately below the useful-area cutoff.
        a=int(3/pixel);image[40:40+a,40:40+a]*=.8
        # A 3.5 x 75 µm strip has sufficient area but no useful wide interior.
        h=int(3.5/pixel);w=int(75/pixel)
        image[160:160+h,80:80+w]*=.8
        rows=analyze(image,pixel)
        assert not any(r['disposition']=='candidate' for r in rows)
        assert any('insufficient_usable_width' in r['filter_reasons'] for r in rows)


def test_diffuse_compact_haze_is_not_a_supported_flake_boundary():
    yy,xx=np.mgrid[:400,:500]
    hump=.12*np.exp(-((xx-250)**2+(yy-200)**2)/(2*42**2))
    image=np.full((400,500,3),2000.,float)*(1-hump[:,:,None])
    rows=analyze(image)
    assert rows
    assert not any(r['disposition']=='candidate' for r in rows)
    assert any('unsupported_or_diffuse_boundary' in r['filter_reasons'] for r in rows)
